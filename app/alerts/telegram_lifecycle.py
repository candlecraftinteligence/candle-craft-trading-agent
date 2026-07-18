from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.analytics.public_signal_quality import (
    MIN_PUBLIC_SIGNAL_GRADE,
    grade_from_score,
    grade_rank,
    normalize_grade,
    public_quality_decision,
)
from app.alerts.telegram_outbox import (
    FAILED_FINAL,
    IN_FLIGHT,
    PENDING,
    POLICY_DISABLED,
    RETRYABLE,
    SENT,
    SKIPPED_DRY_RUN,
    UNCERTAIN,
    SQLitePublicTelegramOutbox,
    persist_intent_parts,
)
from app.alerts.telegram_sender import PublicWatchlistSendGuard, TelegramSender
from app.alerts.telegram_routing import TelegramMessageType
from app.alerts.watchlist_expiry import (
    WATCHLIST_EXPIRY_REASON,
    WATCHLIST_EXPIRY_TIMESTAMP_NA_REASON,
    WATCHLIST_EXPIRY_TIMESTAMP_UNVERIFIED_REASON,
    watchlist_expiry_decision,
)
from app.core.config import Settings
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    SignalMessageContext,
    TelegramAlertType,
    TelegramSignalMessage,
    format_telegram_price,
    format_telegram_signal_message,
)
from app.formatters.scanner_display import build_symbol_display
from app.lifecycle.eligibility import (
    ResearchWatchEligibilityConfig,
    is_internal_touch_state,
    is_public_active_state,
    is_public_signal_eligible_state,
    research_watch_eligible,
)
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason, SetupTransitionResult
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import entry_zone_touched, now_utc_iso
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerSymbolResult
from app.storage.database import DEFAULT_DATABASE_PATH, StorageError, open_initialized_database
from app.storage.models import PublicAlertEventRecord, TelegramAlertAttemptRecord

logger = logging.getLogger(__name__)

PUBLIC_SIGNAL_POLICY_SETUP_ONLY = "setup_only"
CONFIRMED_PUBLIC_DELIVERY_POLICY_DISABLED_REASON = (
    "confirmed_public_delivery_disabled_by_setup_only_policy"
)

WATCH_ALERT_STATES = {
    SetupLifecycleState.WATCHLISTED,
    SetupLifecycleState.STALKING,
    SetupLifecycleState.ACTIONABLE_A_GRADE,
    SetupLifecycleState.A_GRADE_WATCH,
    SetupLifecycleState.TRIGGERED,
}
SIGNAL_ALERT_STATES = {
    SetupLifecycleState.CONFIRMED,
}
PRIOR_ACTIVE_ALERT_TYPES = {
    TelegramAlertType.WATCHLIST.value,
    TelegramAlertType.SIGNAL_CONFIRMED.value,
}
PRIOR_PUBLIC_SIGNAL_ALERT_TYPES = {
    TelegramAlertType.SIGNAL_CONFIRMED.value,
}
TERMINAL_UPDATE_ALERT_TYPES = {
    TelegramAlertType.INVALIDATED,
    TelegramAlertType.EXPIRED,
    TelegramAlertType.NO_LONGER_TRACKING,
}
TERMINAL_COMPLETION_ALERT_TYPES = {
    TelegramAlertType.INVALIDATED.value,
    TelegramAlertType.EXPIRED.value,
    TelegramAlertType.NO_LONGER_TRACKING.value,
    "COOLDOWN",
    TelegramAlertType.SL_HIT.value,
    TelegramAlertType.TP3_HIT.value,
}
WATCHLIST_OUTCOME_TERMINAL_ALERT_TYPES = {
    TelegramAlertType.TP3_HIT,
    TelegramAlertType.SL_HIT,
}
TP_SL_ALERT_TYPES = {
    TelegramAlertType.TP1_HIT,
    TelegramAlertType.TP2_HIT,
    TelegramAlertType.TP3_HIT,
    TelegramAlertType.SL_HIT,
}
WATCHLIST_OUTCOME_TRACKING_ATTEMPT = "WATCHLIST_OUTCOME_TRACKING"
WATCHLIST_TERMINAL_SUPPRESSION_ATTEMPT = "WATCHLIST_TERMINAL_SUPPRESSION"
WATCHLIST_EXPIRY_ATTEMPT = "WATCHLIST_EXPIRY"
RESEARCH_WATCH_DISABLED_REASON = "research_watch_disabled"
RESEARCH_WATCH_PUBLIC_DISABLED_REASON = "research_watch_public_delivery_disabled"
RESEARCH_WATCH_COOLDOWN_REASON = "research_watch_cooldown_active"
RESEARCH_WATCH_SETUP_ONLY_POLICY_DISABLED_REASON = (
    "research_watch_public_delivery_disabled_by_setup_only_policy"
)
SENT_WATCHLIST_RECONCILIATION_ATTEMPT = "SENT_WATCHLIST_RECONCILIATION"
SENT_WATCHLIST_RECONCILIATION_NO_MATCH = "sent_watchlist_reconciliation_no_lifecycle_match"
SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS = "sent_watchlist_reconciliation_ambiguous"
SOFT_FAILED_CONFIRMATION_ATTEMPT = "SOFT_FAILED_CONFIRMATION"
SOFT_FAILED_CONFIRMATION_MIN_OBSERVATIONS = 3
SOFT_FAILED_CONFIRMATION_REMOVAL_REASON = "Watchlist removed because final confirmation conditions did not improve."
MAX_TP_SL_EVENT_DISTANCE_PCT = Decimal("0.25")
MAX_LIVE_PRICE_AGE_SECONDS = Decimal("300")
TP_SL_TRACKING_ACTIVE_STATE_KEYS = {
    "active",
    "executing",
    "managing",
    "sl_hit",
    "tp_hit",
    "tp1_hit",
    "tp2_hit",
    "tp3_hit",
}
TP_SL_ENTRY_TOUCHED_STATE_KEYS = {
    "active",
    "executing",
    "managing",
}
LIVE_PRICE_STALE_FLAG_KEYS = (
    "current_price_stale",
    "price_stale",
    "ticker_stale",
    "live_price_stale",
    "is_stale",
)
LIVE_PRICE_STATUS_KEYS = (
    "current_price_status",
    "price_status",
    "ticker_status",
    "live_price_status",
)
LIVE_PRICE_AGE_KEYS = (
    "current_price_age_seconds",
    "price_age_seconds",
    "ticker_age_seconds",
    "live_price_age_seconds",
)
LIVE_PRICE_TIMESTAMP_KEYS = (
    "current_price_timestamp",
    "price_timestamp",
    "ticker_timestamp",
    "live_price_timestamp",
    "last_price_timestamp",
)
LIVE_PRICE_SYMBOL_KEYS = (
    "current_price_symbol",
    "price_symbol",
    "ticker_symbol",
    "live_price_symbol",
    "last_price_symbol",
)
LIVE_PRICE_VALUE_KEYS = (
    "current_price",
    "ticker_price",
    "live_price",
    "mark_price",
    "last_price",
)
LIVE_PRICE_BLOCKED_STATUS_KEYS = {
    "expired",
    "invalid",
    "missing",
    "nan",
    "stale",
    "unavailable",
    "unreliable",
    "unverified",
    "zero",
}
TERMINAL_IDENTITY_BLOCK_REASONS = {
    "terminal_update_no_prior_public_alert",
    "terminal_update_identity_ambiguous",
    "terminal_update_identity_not_matched",
    "terminal_update_not_public_tracked",
    "terminal_update_not_terminal_state",
}
DEFAULT_CONFIRMED_MIN_RR = Decimal("3")
PUBLIC_SIGNAL_MIN_GRADE = MIN_PUBLIC_SIGNAL_GRADE
PUBLIC_SIGNAL_MIN_SCORE = Decimal("88")
PUBLIC_SIGNAL_MIN_RR = Decimal("3")
PUBLIC_SIGNAL_TARGET_CAUTION_MIN_SCORE = Decimal("88")
PUBLIC_SIGNAL_TARGET_CAUTION_MIN_RR = Decimal("2.8")
PUBLIC_SIGNAL_MIN_TECHNICAL_SCORE = Decimal("95")
PUBLIC_SIGNAL_MIN_OPPORTUNITY_SCORE = Decimal("95")
PUBLIC_SIGNAL_MAX_PER_SCAN = 1
PUBLIC_SIGNAL_MAX_PER_24H = 15
PUBLIC_SIGNAL_MAX_PER_60M = 3
PUBLIC_SIGNAL_SAME_SIDE_COOLDOWN_HOURS = 2
PUBLIC_SIGNAL_OPPOSITE_SIDE_COOLDOWN_MINUTES = 60
PUBLIC_SIGNAL_ROLLING_WINDOW_HOURS = 24
PUBLIC_SIGNAL_ROLLING_WINDOW_MINUTES = 60
PUBLIC_SIGNAL_MAJOR_PRIORITY_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT")
PUBLIC_SIGNAL_NON_CRYPTO_BASE_SYMBOLS = frozenset(
    {
        "SPY",
        "QQQ",
        "XAU",
        "XAG",
        "SKHYNIX",
        "AMD",
        "NVDA",
        "TSLA",
        "EWY",
        "SOXL",
        "DRAM",
        "MRVL",
        "RKLB",
        "MSTR",
        "SAMSUNG",
        "ARM",
        "GLW",
        "SNDK",
        "FLNC",
        "AAOI",
        "HOOD",
        "COIN",
        "IBM",
        "MU",
        "META",
        "AMZN",
        "MSFT",
        "XAUT",
    }
)
PUBLIC_SIGNAL_ALLOWED_TARGET_CAUTION_WARNINGS = frozenset({"target_inside_chop"})
PUBLIC_SIGNAL_NON_PUBLIC_TERMINAL_STATES = frozenset(
    {
        "triggered",
        "rejected",
        "reject",
        "cooldown",
        "cooled_down",
        "invalidated",
        "expired",
        "archived",
        "no_longer_tracking",
        "removed",
        "cancelled",
        "canceled",
    }
)
PUBLIC_SIGNAL_BLOCKED_LIFECYCLE_STATES = frozenset(
    {"triggered", "rejected", "reject", "watchlisted", "watchlist", "stalking", "cooldown", "invalidated", "expired"}
)
PUBLIC_SIGNAL_ALLOWED_ACTIONABILITY_STATES = frozenset({"a_grade_actionable", "a_grade_actionable_target_caution"})
PUBLIC_SIGNAL_HARD_FINAL_GATE_KEYS = frozenset(
    {
        "rr_below_minimum",
        "entry_window_expired",
        "trust_meter_below_minimum",
        "structural_breakdown",
        "body_acceptance_failure",
        "opposing_structure_too_close",
        "opposing_structure",
        "opposing_structure_block",
        "target_integrity",
        "target_integrity_failed",
    }
)
PUBLIC_WATCHLIST_MIN_GRADE = PUBLIC_SIGNAL_MIN_GRADE
PUBLIC_WATCHLIST_MIN_RR = PUBLIC_SIGNAL_MIN_RR
PUBLIC_WATCHLIST_MIN_SCORE = PUBLIC_SIGNAL_MIN_SCORE
PUBLIC_WATCHLIST_MAX_PER_SCAN = PUBLIC_SIGNAL_MAX_PER_SCAN
PUBLIC_WATCHLIST_MAX_PER_24H = PUBLIC_SIGNAL_MAX_PER_24H
PUBLIC_WATCHLIST_MAX_PER_60M = PUBLIC_SIGNAL_MAX_PER_60M
PUBLIC_WATCHLIST_COOLDOWN_HOURS = PUBLIC_SIGNAL_SAME_SIDE_COOLDOWN_HOURS
PUBLIC_WATCHLIST_PRICE_TOLERANCE_TICKS = Decimal("10")
PUBLIC_WATCHLIST_ZONE_TOLERANCE_MULTIPLIER = Decimal("2")
PUBLIC_WATCHLIST_PLAN_ID_VERSION = "public-watchlist-plan-v2"
PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE = "initial_watchlist"
PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON = "duplicate_successful_public_watchlist_event"
PUBLIC_WATCHLIST_RESERVATION_IN_FLIGHT_REASON = "public_watchlist_event_reservation_in_flight"
PUBLIC_WATCHLIST_MISSING_REQUIRED_RESERVATION_REASON = "public_watchlist_missing_required_reservation"
PUBLIC_WATCHLIST_DUPLICATE_EQUIVALENT_PLAN_REASON = "public_watchlist_duplicate_equivalent_plan"
PUBLIC_WATCHLIST_RESERVATION_STATUS = "reserved"
PUBLIC_ALERT_EVENT_RESERVED_STATUS = "RESERVED"
PUBLIC_ALERT_EVENT_SENT_STATUS = "SENT"
PUBLIC_ALERT_EVENT_FAILED_STATUS = "FAILED"
PUBLIC_ALERT_EVENT_BLOCKED_STATUS = "BLOCKED"
DEFAULT_MIN_TECHNICAL_SCORE = Decimal("50")
PUBLIC_WATCHLIST_ELIGIBLE_STATE_KEYS = {
    "watch",
    "watchlist",
    "watchlisted",
    "stalking",
    "actionable_a_grade",
    "a_grade_watch",
}
PUBLIC_WATCHLIST_FIRST_SEEN_TRIGGERED_STATE_KEY = "triggered"
PUBLIC_WATCHLIST_BLOCKED_STATE_KEYS = {
    "invalidated",
    "expired",
    "cooldown",
    "cooled_down",
    "archived",
    "no_longer_tracking",
}
PUBLIC_WATCHLIST_REJECTED_STATE_KEYS = {
    "rejected",
    "reject",
}
A_GRADE_PUBLIC_BLOCKED_ACTIONABILITY_STATES = {
    "a_grade_blocked_by_scoring",
    "a_grade_blocked_by_target",
    "a_grade_blocked_by_entry_window",
    "a_grade_blocked_by_trust",
    "a_grade_blocked_by_final_gates",
}
REGIME_MARKET_CONDITION_PENDING = "REGIME_MARKET_CONDITION_PENDING"
TIMING_CONFIRMATION_PENDING = "TIMING_CONFIRMATION_PENDING"
CONFIRMED_SIGNAL_RR_PENDING = "CONFIRMED_SIGNAL_RR_PENDING"
CONFIRMED_SIGNAL_QUALITY_PENDING = "CONFIRMED_SIGNAL_QUALITY_PENDING"
PUBLIC_WATCHLIST_REGIME_PENDING_GATE_CODES = frozenset()
PUBLIC_WATCHLIST_TIMING_PENDING_GATE_CODES = frozenset(
    {
        "clean_confirmation_pending",
        "confirmation_pending",
        "waiting_for_confirmation",
        "missing_confirmation",
        "missing_confirmation_structure_shift",
        "first_seen_triggered_pre_confirmation",
        "trigger_not_hit",
        "limit_zone_not_hit",
        "limit_zone_not_touched",
        "entry_zone_not_hit",
        "entry_zone_not_touched",
        "pullback_pending",
        "fvg_not_tapped",
        "ob_retest_pending",
        "liquidity_sweep_pending",
        "bos_body_close_pending",
        "choch_confirmation_pending",
        "stalking_not_triggered",
        "limit_zone_hold_pending",
        "pullback_hold_pending",
        "final_quality_gate_pending",
        "quality_gate_pending",
    }
)
PUBLIC_WATCHLIST_MISSING_DATA_GATE_CODES = frozenset(
    {
        "missing_regime_data",
        "regime_data_missing",
        "missing_market_data",
        "market_data_missing",
    }
)
PUBLIC_WATCHLIST_FATAL_GATE_CODES = frozenset(
    {
        "invalid_rr",
        "rr_expansion_needed",
        "wait_for_rr_expansion_above_minimum",
        "missing_rr",
        "missing_stop",
        "missing_sl",
        "missing_stop_loss",
        "missing_target",
        "missing_entry",
        "missing_entry_zone",
        "missing_limit_zone",
        "no_valid_execution_zone",
        "challenge_limit_entry_missing",
        "no_ob_or_fvg_zone",
        "missing_invalidation",
        "no_trade_plan",
        "trade_map_na",
        "bad_data",
        "stale_data",
        "untradable_symbol",
        "low_liquidity",
        "wide_spread",
        "structural_breakdown",
        "body_acceptance_failure",
        "entry_window_expired",
        "hard_regime_block",
        "late_pullback",
        "regime_compatibility",
        "regime_blocked",
        "regime_not_confirmed",
        "market_condition_blocked",
        "market_condition_not_ready",
        "btc_eth_regime_blocked",
        "rejected_by_regime",
        "weak_regime_fit",
        "cooldown",
        "blacklisted_symbol",
        "structural_contradiction",
        "already_invalidated",
        "too_close_to_invalidation",
        "target_inside_chop",
        "target_integrity",
        "target_integrity_failed",
        "active_invalidation",
        "invalidated",
        "no_edge",
    }
)
PUBLIC_WATCHLIST_CONFIRMED_RR_GATE_CODES = frozenset(
    {
        "below_min_rr",
        "confirmed_rr_below_minimum",
        "confirmed_rr_below_min",
        "planned_rr_below_confirmed_minimum",
        "rr_below_confirmed_minimum",
        "rr_below_confirmed_min",
        "rr_below_minimum",
        "rr_too_low",
        "challenge_rr_below_3",
    }
)
PUBLIC_WATCHLIST_CONFIRMED_ONLY_GATE_CODES = frozenset(
    {
        "scoring",
        "rejected_by_scoring",
        "quality_filter",
        "quality_gate_pending",
        "final_quality_gate_pending",
        "technical_score_below_confirmed_minimum",
        "technical_score_below_confirmed_min",
        "technical_score_below_minimum",
        "technical_score_below_min",
        "opportunity_score_below_confirmed_minimum",
        "opportunity_score_below_confirmed_min",
        "opportunity_score_below_scanner_confirmed_minimum",
        "opportunity_score_below_minimum",
        "opportunity_score_below_min",
        "trust_meter_below_confirmed_minimum",
        "trust_meter_below_confirmed_min",
        "trust_meter_below_minimum",
        "challenge_trust_below_85",
    }
)
PUBLIC_WATCHLIST_ALLOWED_GATE_CLASSES = frozenset(
    {
        TIMING_CONFIRMATION_PENDING,
        CONFIRMED_SIGNAL_RR_PENDING,
        CONFIRMED_SIGNAL_QUALITY_PENDING,
    }
)
PUBLIC_WATCHLIST_MALFORMED_FAILED_GATE_CLASS = "MALFORMED_FAILED_GATE"
PUBLIC_WATCHLIST_MISSING_DATA_FAILED_GATE_CLASS = "MISSING_REGIME_MARKET_DATA"
PUBLIC_WATCHLIST_FATAL_FAILED_GATE_CLASS = "FATAL_PUBLIC_WATCHLIST_GATE"
PUBLIC_WATCHLIST_NON_REGIME_FAILED_GATE_CLASS = "NON_REGIME_FAILED_GATE"
PUBLIC_WATCHLIST_UNKNOWN_FAILED_GATE_CLASS = "UNKNOWN_FAILED_GATE"
CONFIRMED_REJECTED_STATUS_KEYS = {
    "scan_error",
    "scanned_no_setup",
    "rejected_by_technical",
    "rejected_by_derivatives",
    "rejected_by_risk",
    "rejected_by_scoring",
    "rejected_by_regime",
    "failed",
    "near_miss",
    "no_setup",
    "rejected",
}
CONFIRMED_ALLOWED_QUALITY_STATE_KEYS = {
    "high_quality_trade",
    "valid_but_lower_quality",
}
WATCHLIST_BLOCKED_QUALITY_STATE_KEYS = {
    "data_issue",
    "rejected_no_edge",
}
WATCHLIST_HARD_STATUS_BLOCKERS = {
    "scan_error",
    "failed",
}
WATCHLIST_ACTION_KEYS = {
    "watchlist_only",
}
WATCHLIST_LIFECYCLE_ACTION_KEYS = {
    "watchlist",
}
WATCHLIST_STALE_OR_INCOMPLETE_GATES = {
    "entry_window_expired",
    "missing_displacement_impulse",
    "no_displacement_candle",
    "missing_stop",
    "pullback_too_deep",
    "pullback_beyond_786",
    "body_acceptance_failure",
    "structural_breakdown",
}
WATCHLIST_OB_FVG_GATES = {
    "no_ob_or_fvg_zone",
    "challenge_limit_entry_missing",
}
WATCHLIST_RR_GATES = {
    "missing_rr",
    "missing_target",
    "rr_below_minimum",
    "challenge_rr_below_3",
    "rr_too_low",
}
WATCHLIST_CONFIRMATION_GATES = {
    "missing_confirmation_structure_shift",
}
PUBLIC_WATCHLIST_KNOWN_NON_REGIME_FAILED_GATES = frozenset(
    {
        "missing_confirmed_sweep",
        "target_integrity",
        "target_integrity_failed",
        "limit_zone_not_touched",
        "trust_meter_below_minimum",
        "challenge_trust_below_85",
        "derivatives_conflict",
        "funding_oi_guard",
        "quality_filter",
        "challenge_illiquid_token",
        "challenge_btc_abnormal",
        "challenge_event_window",
        "btc_volatility_guard",
        "btc_d_guard",
        "event_guard",
        "wick_sweep_reclaim",
    }
    | WATCHLIST_STALE_OR_INCOMPLETE_GATES
    | WATCHLIST_OB_FVG_GATES
    | WATCHLIST_RR_GATES
    | WATCHLIST_CONFIRMATION_GATES
)
INVALIDATION_REJECTION_FRAGMENTS = (
    "technical score",
    "opportunity score",
    "scanner minimum",
    "below scanner",
    "below minimum",
    "rr below",
    "risk/reward below",
    "missing required",
    "missing field",
    "no valid setup",
    "no setup",
    "near miss",
    "rejected setup",
    "setup rejected",
    "failed gate",
    "gate failed",
    "hard rejection",
)
CONFIRMED_TERMINAL_OR_REJECTED_STATES = frozenset(
    {
        "rejected",
        "reject",
        "invalidated",
        "expired",
        "cooldown",
        "cooled_down",
        "no_longer_tracking",
        "removed",
        "cancelled",
        "canceled",
        "archived",
    }
)
ACTIVE_REJECTION_REASON_KEYS = ("active_rejection_reason", "current_rejection_reason")
ACTIVE_REJECTION_REASONS_KEYS = ("active_rejection_reasons", "current_rejection_reasons")
ACTIVE_FAILED_GATE_KEYS = ("active_failed_gate", "current_failed_gate")
ACTIVE_INVALIDATION_REASON_KEYS = ("active_invalidation_reason", "current_invalidation_reason")
HISTORICAL_REJECTION_REASON_KEYS = ("historical_rejection_reason", "previous_rejection_reason")
HISTORICAL_REJECTION_REASONS_KEYS = ("historical_rejection_reasons", "previous_rejection_reasons")
CONFIRMED_ACTIVE_FAILED_GATE_KEYS = frozenset(
    {
        "regime_compatibility",
        "rejected_by_regime",
        "rr_below_minimum",
        "rr_below_min",
        "low_rr",
        "target_integrity",
        "target_integrity_failed",
        "target_expansion",
        "technical_score",
        "technical_quality",
        "opportunity_score",
        "score_below_min",
        "quality_gate",
        "setup_rejected",
        "structural_breakdown",
        "body_acceptance_failure",
        "pullback_too_deep",
        "pullback_beyond_786",
        "entry_window_expired",
        "late_pullback",
        "target_inside_chop",
    }
)


@dataclass(frozen=True)
class TelegramAlertDecision:
    eligible: bool
    reason: str
    alert_type: TelegramAlertType | None = None
    message: TelegramSignalMessage | None = None
    lifecycle_transition: SetupTransitionResult | None = None


@dataclass(frozen=True)
class PublicSignalGateResult:
    allowed: bool
    reason_codes: tuple[str, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    state: str = NA
    setup_id: str | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class ConfirmedSignalRejectionContext:
    active_rejection_reasons: tuple[str, ...] = ()
    historical_rejection_reasons: tuple[str, ...] = ()
    active_failed_gate: str = NA
    historical_failed_gate: str = NA
    active_invalidation_reason: str = NA
    historical_invalidation_reason: str = NA


@dataclass(frozen=True)
class PublicWatchlistGateResult:
    allowed: bool
    setup_id: str | None = None
    symbol: str | None = None
    state: str = NA
    failed_gate_codes: tuple[str, ...] = ()
    failed_gate_classes: tuple[str, ...] = ()
    allowed_missing_gate: str | None = None
    blocking_reasons: tuple[str, ...] = ()
    rr: float | None = None


@dataclass(frozen=True)
class PublicWatchlistCandidate:
    symbol: str = NA
    side: str = NA
    mode: str = NA
    strategy: str = NA
    watchlist_grade: str = NA
    quality_grade_current: str = NA
    grade: str = NA
    readiness_label: str = NA
    readiness_score: str = NA
    potential_rr: Any = NA
    entry_zone_low: Any = NA
    entry_zone_high: Any = NA
    stop_loss: Any = NA
    tp1: Any = NA
    invalidation_level: Any = NA
    pending_confirmation_reason: str = NA
    failed_gate: str = NA
    lifecycle_state: str = NA
    plan_complete: bool = False
    first_seen_triggered_pre_confirmation: bool = False


@dataclass(frozen=True)
class PublicWatchlistPlanIdentity:
    plan_id: str = NA
    plan_hash: str = NA
    symbol: str = NA
    side: str = NA
    setup_family: str = NA
    source_modes: tuple[str, ...] = ()
    structural_identity: tuple[tuple[str, str], ...] = ()
    raw_entry_low: str = NA
    raw_entry_high: str = NA
    raw_invalidation: str = NA
    normalized_entry_low: str = NA
    normalized_entry_high: str = NA
    normalized_invalidation: str = NA
    tick_size: str = NA
    material_change_reason: str = "same_plan_within_price_tolerance"


@dataclass(frozen=True)
class PublicWatchlistReservationResult:
    granted: bool
    event_key: str
    reservation_id: int | None = None
    event_id: int | None = None
    status: str = "skipped"
    reason: str = NA
    detail: str = NA
    matched_prior_alert_id: int | None = None
    matched_prior_event_id: int | None = None
    fallback_matched_legacy_sent: bool = False
    jitter_matched_same_plan: bool = False


@dataclass(frozen=True)
class PublicWatchlistPrefilterResult:
    passed: bool
    blocking_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfirmedAlertPrefilterResult:
    passed: bool
    blocking_reasons: tuple[str, ...] = ()
    reason_buckets: tuple[str, ...] = ()


@dataclass(frozen=True)
class TelegramEligibilityContext:
    min_rr: Decimal = DEFAULT_CONFIRMED_MIN_RR
    min_score_for_idea: Decimal | None = None
    min_technical_score: Decimal = DEFAULT_MIN_TECHNICAL_SCORE
    public_watchlist_enabled: bool = True
    public_watchlist_min_grade: str = PUBLIC_WATCHLIST_MIN_GRADE
    public_watchlist_min_score: Decimal = PUBLIC_WATCHLIST_MIN_SCORE
    public_watchlist_min_rr: Decimal = PUBLIC_WATCHLIST_MIN_RR
    public_watchlist_max_per_24h: int = PUBLIC_WATCHLIST_MAX_PER_24H
    public_watchlist_max_per_60m: int = PUBLIC_WATCHLIST_MAX_PER_60M
    public_watchlist_symbol_whitelist: tuple[str, ...] = ()
    public_watchlist_require_plan: bool = True
    public_watchlist_require_entry_zone: bool = True
    public_watchlist_require_invalidation: bool = True


@dataclass(frozen=True)
class TelegramLifecycleDelivery:
    symbol: str
    signal_id: str
    alert_type: str
    status: str
    detail: str
    message_hash: str = NA
    error_message: str = NA


@dataclass(frozen=True)
class ConfirmedAlertAuditSummary:
    public_signal_policy: str = PUBLIC_SIGNAL_POLICY_SETUP_ONLY
    confirmed_candidates_seen: int = 0
    confirmed_prefilter_passed: int = 0
    confirmed_policy_disabled: int = 0
    signal_confirmed_attempts_created: int = 0
    signal_confirmed_sent: int = 0
    policy_disabled_by_reason: Mapping[str, int] = field(default_factory=dict)
    blocked_before_attempt_by_reason: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TelegramLifecycleDeliverySummary:
    attempted: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    blocked: int = 0
    blocked_repeat: int = 0
    duplicate: int = 0
    ineligible: int = 0
    deliveries: tuple[TelegramLifecycleDelivery, ...] = ()
    public_watchlist_audit: PublicWatchlistAuditSummary = field(default_factory=lambda: PublicWatchlistAuditSummary())
    confirmed_alert_audit: ConfirmedAlertAuditSummary = field(default_factory=lambda: ConfirmedAlertAuditSummary())


@dataclass(frozen=True)
class PublicWatchlistCandidateAudit:
    symbol: str
    eligible: bool
    field_prefilter_passed: bool = False
    first_seen_triggered_pre_confirmation: bool = False
    state: str = NA
    core_lifecycle_state: str = NA
    derived_public_watchlist_state: str = NA
    reject_reasons: tuple[str, ...] = ()
    active_fatal_reasons: tuple[str, ...] = ()
    confirmed_only_pending_reasons: tuple[str, ...] = ()
    watchlist_eligibility_result: str = NA
    public_decision: str = NA
    public_block_reason: str = NA
    actionable_grade_reason: str = NA
    confirmation_block_reason: str = NA
    lifecycle_promotion_reason: str = NA
    plan_hash: str = NA
    public_watchlist_plan_id: str = NA
    side: str = NA
    source_modes: tuple[str, ...] = ()
    raw_zone: str = NA
    normalized_zone: str = NA
    raw_invalidation: str = NA
    normalized_invalidation: str = NA
    structural_identity_fields: Mapping[str, str] = field(default_factory=dict)
    previous_successful_alert_id: str = NA
    dedupe_decision: str = NA
    material_change_reason: str = NA
    prior_public_alert_status: str = NA
    rr: str = NA
    grade: str = NA
    has_side_grade: bool = False
    has_rr: bool = False
    has_zone: bool = False
    has_invalidation: bool = False
    plan_complete: bool = False
    explicit_source: bool = False
    historical_rejection_only_ignored: bool = False
    delivery_status: str = NA
    skip_reason: str = NA


@dataclass(frozen=True)
class PublicWatchlistAuditSummary:
    source_candidates_seen: int = 0
    field_prefilter_passed: int = 0
    eligible_watch_or_stalking: int = 0
    eligible_first_seen_triggered_pre_confirmation: int = 0
    blocked_before_attempt: int = 0
    blocked_after_attempt: int = 0
    blocked_by_reason: Mapping[str, int] = field(default_factory=dict)
    near_miss_seen: int = 0
    near_miss_with_side_grade: int = 0
    near_miss_with_rr: int = 0
    near_miss_with_entry_zone: int = 0
    near_miss_with_invalidation: int = 0
    near_miss_plan_complete: int = 0
    public_watchlist_trade_ideas_created: int = 0
    public_watchlist_alerts_created: int = 0
    public_watchlist_sent: int = 0
    complete_candidates_seen: int = 0
    eligible_by_watchlist_policy: int = 0
    blocked_by_fatal_reason: int = 0
    blocked_by_grade: int = 0
    blocked_by_rr: int = 0
    blocked_by_missing_zone: int = 0
    blocked_by_missing_invalidation: int = 0
    allowed_confirmed_only_scoring_failure: int = 0
    watch_state_candidates: int = 0
    triggered_waiting_confirmation_candidates: int = 0
    rejected_core_but_watchlist_eligible: int = 0
    candidates_with_prior_successful_alert: int = 0
    candidates_without_prior_successful_alert: int = 0
    attempts_created: int = 0
    deduped: int = 0
    cooldown: int = 0
    candidates_considered: int = 0
    eligible: int = 0
    sent: int = 0
    skipped_by_reason: Mapping[str, int] = field(default_factory=dict)
    candidates: tuple[PublicWatchlistCandidateAudit, ...] = ()

    @property
    def public_watchlist_bridge(self) -> Mapping[str, Any]:
        return {
            "near_miss_seen": self.near_miss_seen,
            "near_miss_with_side_grade": self.near_miss_with_side_grade,
            "near_miss_with_rr": self.near_miss_with_rr,
            "near_miss_with_entry_zone": self.near_miss_with_entry_zone,
            "near_miss_with_invalidation": self.near_miss_with_invalidation,
            "near_miss_plan_complete": self.near_miss_plan_complete,
            "public_watchlist_trade_ideas_created": self.public_watchlist_trade_ideas_created,
            "public_watchlist_alerts_created": self.public_watchlist_alerts_created,
            "public_watchlist_sent": self.public_watchlist_sent,
            "blocked_by_reason": dict(self.blocked_by_reason),
        }

    @property
    def public_watchlist_reconciliation_audit(self) -> Mapping[str, Any]:
        return {
            "complete_candidates_seen": self.complete_candidates_seen,
            "eligible_by_watchlist_policy": self.eligible_by_watchlist_policy,
            "blocked_by_fatal_reason": self.blocked_by_fatal_reason,
            "blocked_by_grade": self.blocked_by_grade,
            "blocked_by_rr": self.blocked_by_rr,
            "blocked_by_missing_zone": self.blocked_by_missing_zone,
            "blocked_by_missing_invalidation": self.blocked_by_missing_invalidation,
            "allowed_confirmed_only_scoring_failure": self.allowed_confirmed_only_scoring_failure,
            "watch_state_candidates": self.watch_state_candidates,
            "triggered_waiting_confirmation_candidates": self.triggered_waiting_confirmation_candidates,
            "rejected_core_but_watchlist_eligible": self.rejected_core_but_watchlist_eligible,
            "candidates_with_prior_successful_alert": self.candidates_with_prior_successful_alert,
            "candidates_without_prior_successful_alert": self.candidates_without_prior_successful_alert,
            "attempts_created": self.attempts_created,
            "sent": self.sent,
            "blocked_after_attempt": self.blocked_after_attempt,
            "deduped": self.deduped,
            "cooldown": self.cooldown,
        }

    @property
    def public_watchlist_dedupe_audit(self) -> Mapping[str, Any]:
        canonical_plan_ids = {
            audit.public_watchlist_plan_id for audit in self.candidates if audit.public_watchlist_plan_id != NA
        }
        duplicate_sources_collapsed = max(0, len(self.candidates) - len(canonical_plan_ids))
        return {
            "eligible_candidates": self.eligible,
            "canonical_plans": len(canonical_plan_ids),
            "duplicate_candidate_sources_collapsed": duplicate_sources_collapsed,
            "prior_successful_alert_found": self.candidates_with_prior_successful_alert,
            "reconciliation_duplicates_skipped": sum(
                1 for audit in self.candidates if _public_watchlist_duplicate_skip_reason(audit.skip_reason)
            ),
            "lifecycle_duplicates_skipped": sum(
                1 for audit in self.candidates if audit.delivery_status == "duplicate"
            ),
            "cross_mode_duplicates_collapsed": duplicate_sources_collapsed,
            "new_initial_watchlists_sent": self.sent,
            "limit_hit_updates_sent": 0,
            "material_plan_changes": sum(
                1 for audit in self.candidates if audit.material_change_reason.startswith("material_")
            ),
            "blocked_attempts_retried": 0,
            "cooldown_skips": self.cooldown,
        }

    @property
    def public_watchlist_hard_dedupe_audit(self) -> Mapping[str, Any]:
        canonical_plan_ids = {
            audit.public_watchlist_plan_id for audit in self.candidates if audit.public_watchlist_plan_id != NA
        }
        duplicate_sources_collapsed = max(0, len(self.candidates) - len(canonical_plan_ids))
        skipped_prior_success = sum(
            1 for audit in self.candidates if _public_watchlist_duplicate_skip_reason(audit.skip_reason)
        )
        failed_retryable = sum(
            1 for audit in self.candidates if audit.delivery_status in {"failed", "skipped"} and not _public_watchlist_duplicate_skip_reason(audit.skip_reason) and audit.skip_reason != "public_watchlist_max_per_scan_reached"
        )
        return {
            "eligible_candidates": self.eligible,
            "canonical_plan_ids": tuple(sorted(canonical_plan_ids)),
            "reservations_attempted": self.sent + skipped_prior_success + failed_retryable,
            "reservations_granted": self.sent + failed_retryable,
            "skipped_prior_successful_event": skipped_prior_success,
            "fallback_matched_legacy_sent": sum(1 for audit in self.candidates if "fallback_matched_legacy_sent" in audit.skip_reason),
            "cross_mode_collapsed": duplicate_sources_collapsed,
            "jitter_matched_same_plan": sum(1 for audit in self.candidates if "jitter_matched_same_plan" in audit.skip_reason),
            "sent_initial_watchlists": self.sent,
            "failed_sends_retryable": failed_retryable,
            "failed_sends_nonretryable": 0,
        }


@dataclass(frozen=True)
class ResearchWatchCandidate:
    symbol_result: ScannerSymbolResult
    message: TelegramSignalMessage
    signal_id_prefix: str
    quality_score: int
    readiness_score: int
    regime_state: str
    regime_compatibility_label: str
    regime_confidence: str = NA
    next_trigger: str = NA
    action_label: str = NA
    rejection_reason: str = NA


@dataclass(frozen=True)
class TerminalIdentityBridge:
    prior_alert: TelegramAlertAttemptRecord | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class SentWatchlistLifecycleMatch:
    record: SetupLifecycleRecord | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class SentWatchlistReconciliationOutcome:
    alert_type: TelegramAlertType
    message: TelegramSignalMessage
    symbol_result: ScannerSymbolResult


@dataclass(frozen=True)
class WatchlistCandleSnapshot:
    high: Decimal
    low: Decimal
    identity: str = NA


@dataclass(frozen=True)
class WatchlistLivePriceSnapshot:
    symbol: str
    price: Decimal
    source: str = NA
    timestamp: str = NA


class SQLiteTelegramAlertAttemptRepository(AbstractContextManager["SQLiteTelegramAlertAttemptRepository"]):
    def __init__(self, database_path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        self.database_path = Path(database_path)
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> SQLiteTelegramAlertAttemptRepository:
        self.connection = open_initialized_database(self.database_path)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self.connection is None:
            return
        if exc_type is None:
            self.connection.commit()
        self.connection.close()
        self.connection = None

    def commit_before_network_activity(self) -> None:
        """End any pending write transaction before a sender can perform I/O."""

        try:
            self._connection.commit()
        except sqlite3.Error as exc:
            raise StorageError(
                "Unable to commit Telegram repository state before network activity."
            ) from exc



    def has_canonical_lifecycle_outcome_progress(self, *, lifecycle_id: str) -> bool:
        normalized = _text(lifecycle_id)
        if normalized == NA:
            return False
        row = self._connection.execute(
            """
            SELECT 1 FROM setup_lifecycle_outcome_progress
            WHERE lifecycle_id = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        return row is not None

    def has_attempt(self, *, signal_id: str, alert_type: TelegramAlertType | str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM telegram_alert_attempts
            WHERE signal_id = ? AND alert_type = ?
            LIMIT 1
            """,
            (_identity(signal_id), _alert_type_text(alert_type)),
        ).fetchone()
        return row is not None

    def get_attempt(
        self,
        *,
        signal_id: str,
        alert_type: TelegramAlertType | str,
    ) -> TelegramAlertAttemptRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM telegram_alert_attempts
            WHERE signal_id = ? AND alert_type = ?
            LIMIT 1
            """,
            (_identity(signal_id), _alert_type_text(alert_type)),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def has_prior_active_alert(self, *, signal_id: str) -> bool:
        return self.get_prior_public_alert(signal_ids=(signal_id,)) is not None

    def has_prior_public_signal_alert(self, *, signal_id: str) -> bool:
        return self.get_prior_public_signal_alert(signal_ids=(signal_id,)) is not None

    def get_prior_public_signal_alert(
        self,
        *,
        signal_ids: Sequence[str],
    ) -> TelegramAlertAttemptRecord | None:
        candidates = tuple(dict.fromkeys(_identity(signal_id) for signal_id in signal_ids if _identity(signal_id) != NA))
        if not candidates:
            return None
        placeholders = ",".join("?" for _ in candidates)
        type_placeholders = ",".join("?" for _ in PRIOR_PUBLIC_SIGNAL_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT * FROM telegram_alert_attempts
            WHERE signal_id IN ({placeholders})
              AND alert_type IN ({type_placeholders})
              AND telegram_status = 'sent'
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            ORDER BY id ASC
            """,
            (*candidates, *sorted(PRIOR_PUBLIC_SIGNAL_ALERT_TYPES)),
        ).fetchall()
        records = tuple(_record_from_row(row) for row in rows)
        if not records:
            return None
        return _preferred_prior_active_record(records, candidates)

    def get_prior_public_alert(
        self,
        *,
        signal_ids: Sequence[str],
        symbol: str | None = None,
        direction: str | None = None,
    ) -> TelegramAlertAttemptRecord | None:
        candidates = tuple(dict.fromkeys(_identity(signal_id) for signal_id in signal_ids if _identity(signal_id) != NA))
        if candidates:
            placeholders = ",".join("?" for _ in candidates)
            type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
            rows = self._connection.execute(
                f"""
                SELECT * FROM telegram_alert_attempts
                WHERE signal_id IN ({placeholders})
                  AND alert_type IN ({type_placeholders})
                  AND telegram_status = 'sent'
                  AND sent_at IS NOT NULL
                  AND sent_at NOT IN ('', 'N/A')
                ORDER BY id ASC
                """,
                (*candidates, *sorted(PRIOR_ACTIVE_ALERT_TYPES)),
            ).fetchall()
            records = tuple(_record_from_row(row) for row in rows)
            if records:
                return _preferred_prior_active_record(records, candidates)
        return None

    def list_active_prior_public_alerts(
        self,
        *,
        symbol: str,
        direction: str | None = None,
    ) -> tuple[TelegramAlertAttemptRecord, ...]:
        normalized_symbol = _symbol(symbol)
        if normalized_symbol == NA:
            return ()
        params: list[Any] = [
            normalized_symbol,
            *sorted(PRIOR_ACTIVE_ALERT_TYPES),
            *sorted(TERMINAL_COMPLETION_ALERT_TYPES),
        ]
        direction_clause = ""
        normalized_direction = _text(direction)
        if normalized_direction != NA:
            direction_clause = "AND prior.direction = ?"
            params.append(normalized_direction)
        type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        terminal_placeholders = ",".join("?" for _ in TERMINAL_COMPLETION_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT prior.* FROM telegram_alert_attempts AS prior
            WHERE prior.symbol = ?
              AND prior.alert_type IN ({type_placeholders})
              AND prior.telegram_status = 'sent'
              AND prior.sent_at IS NOT NULL
              AND prior.sent_at NOT IN ('', 'N/A')
              AND NOT EXISTS (
                  SELECT 1 FROM telegram_alert_attempts AS terminal
                  WHERE terminal.signal_id = prior.signal_id
                    AND terminal.alert_type IN ({terminal_placeholders})
                    AND terminal.telegram_status = 'sent'
                    AND terminal.sent_at IS NOT NULL
                    AND terminal.sent_at NOT IN ('', 'N/A')
              )
              {direction_clause}
            ORDER BY prior.id ASC
            """,
            params,
        ).fetchall()
        return _unique_prior_public_records(tuple(_record_from_row(row) for row in rows))

    def list_prior_public_alerts(
        self,
        *,
        symbol: str,
        direction: str | None = None,
    ) -> tuple[TelegramAlertAttemptRecord, ...]:
        normalized_symbol = _symbol(symbol)
        if normalized_symbol == NA:
            return ()
        params: list[Any] = [normalized_symbol, *sorted(PRIOR_ACTIVE_ALERT_TYPES)]
        direction_clause = ""
        normalized_direction = _text(direction)
        if normalized_direction != NA:
            direction_clause = "AND direction = ?"
            params.append(normalized_direction)
        type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT * FROM telegram_alert_attempts
            WHERE symbol = ?
              AND alert_type IN ({type_placeholders})
              AND telegram_status = 'sent'
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
              {direction_clause}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return _unique_prior_public_records(tuple(_record_from_row(row) for row in rows))

    def has_prior_public_alert_for_symbol(self, *, symbol: str) -> bool:
        type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        row = self._connection.execute(
            f"""
            SELECT 1 FROM telegram_alert_attempts
            WHERE symbol = ?
              AND alert_type IN ({type_placeholders})
              AND telegram_status = 'sent'
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            LIMIT 1
            """,
            (_symbol(symbol), *sorted(PRIOR_ACTIVE_ALERT_TYPES)),
        ).fetchone()
        return row is not None

    def has_sent_terminal_outcome(self, *, signal_id: str) -> bool:
        placeholders = ",".join("?" for _ in TERMINAL_COMPLETION_ALERT_TYPES)
        row = self._connection.execute(
            f"""
            SELECT 1 FROM telegram_alert_attempts
            WHERE signal_id = ?
              AND alert_type IN ({placeholders})
              AND telegram_status = 'sent'
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            LIMIT 1
            """,
            (_identity(signal_id), *sorted(TERMINAL_COMPLETION_ALERT_TYPES)),
        ).fetchone()
        return row is not None

    def list_sent_watchlist_alerts(self) -> tuple[TelegramAlertAttemptRecord, ...]:
        terminal_placeholders = ",".join("?" for _ in TERMINAL_COMPLETION_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT watch.* FROM telegram_alert_attempts AS watch
            WHERE watch.alert_type = ?
              AND watch.telegram_status = 'sent'
              AND watch.sent_at IS NOT NULL
              AND watch.sent_at NOT IN ('', 'N/A')
              AND NOT EXISTS (
                  SELECT 1 FROM telegram_alert_attempts AS terminal
                  WHERE terminal.signal_id = watch.signal_id
                    AND terminal.alert_type IN ({terminal_placeholders})
                    AND terminal.telegram_status = 'sent'
                    AND terminal.sent_at IS NOT NULL
                    AND terminal.sent_at NOT IN ('', 'N/A')
              )
            ORDER BY watch.id ASC
            """,
            (TelegramAlertType.WATCHLIST.value, *sorted(TERMINAL_COMPLETION_ALERT_TYPES)),
        ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def has_recent_research_watch_alert(self, *, symbol: str, since: str) -> bool:
        normalized_symbol = _research_cooldown_symbol(symbol)
        if normalized_symbol == NA:
            return False
        cutoff = _parse_iso_datetime(since)
        rows = self._connection.execute(
            """
            SELECT symbol, sent_at FROM telegram_alert_attempts
            WHERE alert_type = ?
              AND telegram_status = 'sent'
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            """,
            (TelegramAlertType.RESEARCH_WATCH.value,),
        ).fetchall()
        for row in rows:
            if _research_cooldown_symbol(row["symbol"]) != normalized_symbol:
                continue
            if _parse_iso_datetime(row["sent_at"]) >= cutoff:
                return True
        return False

    def has_recent_public_watchlist_alert(
        self,
        *,
        symbol: str,
        direction: Any,
        cooldown_key: str,
        since: str,
        signal_id: str | None = None,
    ) -> bool:
        normalized_symbol = _symbol(symbol)
        normalized_direction = _status_key(direction)
        normalized_signal_id = _identity(signal_id)
        if normalized_symbol == NA or normalized_direction not in {"long", "short"}:
            return False
        if _text(cooldown_key) == NA and normalized_signal_id == NA:
            return False
        cutoff = _parse_iso_datetime(since)
        rows = self._connection.execute(
            """
            SELECT signal_id, symbol, direction, entry_low, entry_high, stop_loss, tp1, tp2, tp3,
                   public_watchlist_plan_id, public_watchlist_event_key, sent_at
            FROM telegram_alert_attempts
            WHERE alert_type = ?
              AND telegram_status = 'sent'
              AND symbol = ?
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            """,
            (TelegramAlertType.WATCHLIST.value, normalized_symbol),
        ).fetchall()
        for row in rows:
            if _status_key(row["direction"]) != normalized_direction:
                continue
            if _parse_iso_datetime(row["sent_at"]) < cutoff:
                continue
            if _public_watchlist_plan_row_matches(row, cooldown_key=cooldown_key, signal_id=normalized_signal_id):
                return True
        return False

    def count_recent_public_signal_alerts(self, *, since: str) -> int:
        cutoff = _parse_iso_datetime(since)
        type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT sent_at FROM telegram_alert_attempts
            WHERE alert_type IN ({type_placeholders})
              AND telegram_status = 'sent'
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            """,
            tuple(sorted(PRIOR_ACTIVE_ALERT_TYPES)),
        ).fetchall()
        return sum(1 for row in rows if _parse_iso_datetime(row["sent_at"]) >= cutoff)

    def has_recent_public_signal_for_symbol(self, *, symbol: str, since: str) -> bool:
        normalized_symbol = _symbol(symbol)
        if normalized_symbol == NA:
            return False
        cutoff = _parse_iso_datetime(since)
        type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT sent_at FROM telegram_alert_attempts
            WHERE alert_type IN ({type_placeholders})
              AND telegram_status = 'sent'
              AND symbol = ?
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            """,
            (*sorted(PRIOR_ACTIVE_ALERT_TYPES), normalized_symbol),
        ).fetchall()
        return any(_parse_iso_datetime(row["sent_at"]) >= cutoff for row in rows)

    def has_recent_public_signal_for_symbol_direction(self, *, symbol: str, direction: Any, since: str) -> bool:
        normalized_symbol = _symbol(symbol)
        normalized_direction = _status_key(direction)
        if normalized_symbol == NA or normalized_direction not in {"long", "short"}:
            return False
        cutoff = _parse_iso_datetime(since)
        type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT sent_at, direction FROM telegram_alert_attempts
            WHERE alert_type IN ({type_placeholders})
              AND telegram_status = 'sent'
              AND symbol = ?
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            """,
            (*sorted(PRIOR_ACTIVE_ALERT_TYPES), normalized_symbol),
        ).fetchall()
        return any(
            _status_key(row["direction"]) == normalized_direction and _parse_iso_datetime(row["sent_at"]) >= cutoff
            for row in rows
        )

    def has_prior_public_watchlist_plan_alert(
        self,
        *,
        symbol: str,
        direction: Any,
        cooldown_key: str,
        signal_id: str | None = None,
    ) -> bool:
        normalized_symbol = _symbol(symbol)
        normalized_direction = _status_key(direction)
        normalized_signal_id = _identity(signal_id)
        if normalized_symbol == NA or normalized_direction not in {"long", "short"}:
            return False
        if _text(cooldown_key) == NA and normalized_signal_id == NA:
            return False
        rows = self._connection.execute(
            """
            SELECT signal_id, symbol, direction, entry_low, entry_high, stop_loss, tp1, tp2, tp3,
                   public_watchlist_plan_id, public_watchlist_event_key, sent_at
            FROM telegram_alert_attempts
            WHERE alert_type = ?
              AND telegram_status = 'sent'
              AND symbol = ?
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            """,
            (TelegramAlertType.WATCHLIST.value, normalized_symbol),
        ).fetchall()
        for row in rows:
            if _status_key(row["direction"]) != normalized_direction:
                continue
            if _public_watchlist_plan_row_matches(row, cooldown_key=cooldown_key, signal_id=normalized_signal_id):
                return True
        return False

    def public_watchlist_plan_attempt_status(
        self,
        *,
        symbol: str,
        direction: Any,
        cooldown_key: str,
        signal_id: str | None = None,
    ) -> str:
        normalized_symbol = _symbol(symbol)
        normalized_direction = _status_key(direction)
        normalized_signal_id = _identity(signal_id)
        if normalized_symbol == NA or normalized_direction not in {"long", "short"}:
            return NA
        if _text(cooldown_key) == NA and normalized_signal_id == NA:
            return NA
        rows = self._connection.execute(
            """
            SELECT signal_id, symbol, direction, entry_low, entry_high, stop_loss, tp1, tp2, tp3,
                   public_watchlist_plan_id, public_watchlist_event_key,
                   telegram_status, attempted_alert_type, alert_type, id
            FROM telegram_alert_attempts
            WHERE symbol = ?
              AND (alert_type = ? OR attempted_alert_type = ?)
            ORDER BY id DESC
            """,
            (normalized_symbol, TelegramAlertType.WATCHLIST.value, TelegramAlertType.WATCHLIST.value),
        ).fetchall()
        matched_status: str = NA
        for row in rows:
            if _status_key(row["direction"]) != normalized_direction:
                continue
            if not _public_watchlist_plan_row_matches(row, cooldown_key=cooldown_key, signal_id=normalized_signal_id):
                continue
            status = _text(row["telegram_status"])
            if status == "sent":
                return status
            if matched_status == NA:
                matched_status = status
        return matched_status

    def get_public_watchlist_event_attempt(
        self,
        *,
        event_key: str,
        statuses: Sequence[str] | None = None,
    ) -> TelegramAlertAttemptRecord | None:
        normalized_event_key = _text(event_key)
        if normalized_event_key == NA:
            return None
        params: list[Any] = [normalized_event_key]
        status_clause = ""
        if statuses:
            normalized_statuses = tuple(_text(status) for status in statuses if _text(status) != NA)
            if normalized_statuses:
                placeholders = ",".join("?" for _ in normalized_statuses)
                status_clause = f"AND telegram_status IN ({placeholders})"
                params.extend(normalized_statuses)
        row = self._connection.execute(
            f"""
            SELECT * FROM telegram_alert_attempts
            WHERE public_watchlist_event_key = ?
              {status_clause}
            ORDER BY id ASC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def find_successful_public_watchlist_event(
        self,
        *,
        event_key: str,
    ) -> TelegramAlertAttemptRecord | None:
        return self.get_public_watchlist_event_attempt(event_key=event_key, statuses=("sent",))

    def find_matching_successful_public_watchlist_event(
        self,
        *,
        symbol: str,
        direction: Any,
        plan: PublicWatchlistPlanIdentity,
        event_type: str,
        since: str | None = None,
    ) -> tuple[TelegramAlertAttemptRecord | None, bool, bool]:
        normalized_symbol = _symbol(symbol)
        normalized_direction = _status_key(direction)
        if normalized_symbol == NA or normalized_direction not in {"long", "short"}:
            return None, False, False
        cutoff = _parse_iso_datetime(since) if _text(since) != NA else None
        rows = self._connection.execute(
            """
            SELECT * FROM telegram_alert_attempts
            WHERE symbol = ?
              AND telegram_status = 'sent'
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
              AND (alert_type = ? OR attempted_alert_type = ?)
            ORDER BY id ASC
            """,
            (normalized_symbol, TelegramAlertType.WATCHLIST.value, TelegramAlertType.WATCHLIST.value),
        ).fetchall()
        event_key = _public_watchlist_event_key(plan.plan_id, event_type)
        for row in rows:
            record = _record_from_row(row)
            if _status_key(record.direction) != normalized_direction:
                continue
            if cutoff is not None and _parse_iso_datetime(record.sent_at) < cutoff:
                continue
            if _text(record.public_watchlist_event_key) == event_key or _text(record.public_watchlist_plan_id) == plan.plan_id:
                return record, False, False
            matched, jitter = _public_watchlist_legacy_record_matches_plan(record, plan)
            if matched:
                return record, True, jitter
        return None, False, False

    def replace_attempt_with_reservation(self, *, attempt_id: int, record: TelegramAlertAttemptRecord) -> bool:
        now = _text(record.attempted_at)
        if now == NA:
            now = now_utc_iso()
        cursor = self._connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET
                symbol = ?,
                direction = ?,
                previous_state = ?,
                new_state = ?,
                alert_type = ?,
                lifecycle_state = ?,
                sent_at = NULL,
                attempted_at = ?,
                telegram_status = ?,
                message_hash = ?,
                scan_run_id = ?,
                attempted_alert_type = ?,
                setup_quality_score = ?,
                rr_planned = ?,
                min_rr = ?,
                opportunity_score = ?,
                min_score_for_idea = ?,
                technical_score = ?,
                price_level = ?,
                entry_low = ?,
                entry_high = ?,
                stop_loss = ?,
                tp1 = ?,
                tp2 = ?,
                tp3 = ?,
                public_watchlist_plan_id = ?,
                public_watchlist_event_key = ?,
                public_alert_event_type = ?,
                normalized_entry_zone_low = ?,
                normalized_entry_zone_high = ?,
                normalized_invalidation = ?,
                dedupe_status = ?,
                dedupe_reason = ?,
                blocked_reason = ?,
                error_message = ?,
                invalid_target_fields = ?,
                first_seen_at = CASE
                    WHEN first_seen_at IS NULL OR first_seen_at = 'N/A' OR first_seen_at = '' THEN ?
                    ELSE first_seen_at
                END,
                last_seen_at = ?,
                seen_count = CASE
                    WHEN seen_count IS NULL OR seen_count < 1 THEN 1
                    ELSE seen_count + 1
                END,
                last_scan_run_id = ?,
                last_error_message = ?
            WHERE id = ?
            """,
            (
                _symbol(record.symbol),
                _text(record.direction),
                _text(record.previous_state),
                _text(record.new_state),
                _text(record.alert_type),
                _text(record.lifecycle_state),
                now,
                _text(record.telegram_status),
                _text(record.message_hash),
                record.scan_run_id,
                _text(record.attempted_alert_type),
                _text(record.setup_quality_score),
                _text(record.rr_planned),
                _text(record.min_rr),
                _text(record.opportunity_score),
                _text(record.min_score_for_idea),
                _text(record.technical_score),
                _text(record.price_level),
                _text(record.entry_low),
                _text(record.entry_high),
                _text(record.stop_loss),
                _text(record.tp1),
                _text(record.tp2),
                _text(record.tp3),
                _text(record.public_watchlist_plan_id),
                _text(record.public_watchlist_event_key),
                _text(record.public_alert_event_type),
                _text(record.normalized_entry_zone_low),
                _text(record.normalized_entry_zone_high),
                _text(record.normalized_invalidation),
                _text(record.dedupe_status),
                _text(record.dedupe_reason),
                _text(record.blocked_reason),
                _text(record.error_message),
                _text(record.invalid_target_fields),
                now,
                now,
                record.last_scan_run_id or record.scan_run_id,
                _text(record.last_error_message),
                int(attempt_id),
            ),
        )
        return cursor.rowcount > 0

    def mark_public_watchlist_reservation_result(
        self,
        *,
        attempt_id: int,
        status: str,
        sent_at: str | None,
        message_hash: str,
        error_message: str,
        dedupe_status: str,
        dedupe_reason: str,
    ) -> bool:
        now = now_utc_iso()
        cursor = self._connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET
                sent_at = ?,
                telegram_status = ?,
                message_hash = ?,
                blocked_reason = ?,
                error_message = ?,
                last_seen_at = ?,
                last_error_message = ?,
                dedupe_status = ?,
                dedupe_reason = ?
            WHERE id = ?
            """,
            (
                sent_at,
                _text(status),
                _text(message_hash),
                NA if _text(status) == "sent" else _text(error_message),
                _text(error_message),
                now,
                _text(error_message),
                _text(dedupe_status),
                _text(dedupe_reason),
                int(attempt_id),
            ),
        )
        return cursor.rowcount > 0

    def insert_attempt(self, record: TelegramAlertAttemptRecord) -> bool:
        telegram_status = _text(record.telegram_status)
        attempted_at = _text(record.attempted_at)
        if attempted_at == NA:
            attempted_at = _text(record.sent_at)
        if attempted_at == NA:
            attempted_at = now_utc_iso()
        sent_at: str | None
        if telegram_status == "sent":
            sent_at = _text(record.sent_at)
            if sent_at == NA:
                sent_at = attempted_at
        else:
            sent_at = None
        first_seen_at = _text(record.first_seen_at)
        if first_seen_at == NA:
            first_seen_at = _text(sent_at)
        if first_seen_at == NA:
            first_seen_at = attempted_at
        last_seen_at = _text(record.last_seen_at)
        if last_seen_at == NA:
            last_seen_at = _text(sent_at)
        if last_seen_at == NA:
            last_seen_at = attempted_at
        last_error_message = _text(record.last_error_message)
        if last_error_message == NA:
            last_error_message = _text(record.error_message)
        try:
            self._connection.execute(
                """
                INSERT INTO telegram_alert_attempts (
                    signal_id, symbol, direction, previous_state, new_state,
                    alert_type, lifecycle_state, sent_at, attempted_at, telegram_status,
                    message_hash, scan_run_id, attempted_alert_type, setup_quality_score,
                    rr_planned, min_rr, opportunity_score, min_score_for_idea,
                    technical_score, price_level, entry_low, entry_high, stop_loss,
                    tp1, tp2, tp3, public_watchlist_plan_id, public_watchlist_event_key,
                    public_alert_event_type, normalized_entry_zone_low, normalized_entry_zone_high,
                    normalized_invalidation, dedupe_status, dedupe_reason,
                    blocked_reason, error_message, invalid_target_fields, first_seen_at, last_seen_at,
                    seen_count, last_scan_run_id, last_error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _identity(record.signal_id),
                    _symbol(record.symbol),
                    _text(record.direction),
                    _text(record.previous_state),
                    _text(record.new_state),
                    _text(record.alert_type),
                    _text(record.lifecycle_state),
                    sent_at,
                    attempted_at,
                    telegram_status,
                    _text(record.message_hash),
                    record.scan_run_id,
                    _text(record.attempted_alert_type),
                    _text(record.setup_quality_score),
                    _text(record.rr_planned),
                    _text(record.min_rr),
                    _text(record.opportunity_score),
                    _text(record.min_score_for_idea),
                    _text(record.technical_score),
                    _text(record.price_level),
                    _text(record.entry_low),
                    _text(record.entry_high),
                    _text(record.stop_loss),
                    _text(record.tp1),
                    _text(record.tp2),
                    _text(record.tp3),
                    _text(record.public_watchlist_plan_id),
                    _text(record.public_watchlist_event_key),
                    _text(record.public_alert_event_type),
                    _text(record.normalized_entry_zone_low),
                    _text(record.normalized_entry_zone_high),
                    _text(record.normalized_invalidation),
                    _text(record.dedupe_status),
                    _text(record.dedupe_reason),
                    _text(record.blocked_reason),
                    _text(record.error_message),
                    _text(record.invalid_target_fields),
                    first_seen_at,
                    last_seen_at,
                    int(record.seen_count) if record.seen_count >= 1 else 1,
                    record.last_scan_run_id or record.scan_run_id,
                    last_error_message,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def compact_repeated_attempt(self, record: TelegramAlertAttemptRecord) -> bool:
        status = _text(record.telegram_status)
        if status not in {"blocked", "skipped"}:
            return False
        now = _text(record.last_seen_at if _text(record.last_seen_at) != NA else now_utc_iso())
        cursor = self._connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET
                attempted_at = CASE
                    WHEN attempted_at IS NULL OR attempted_at = 'N/A' OR attempted_at = ''
                        THEN ?
                    ELSE attempted_at
                END,
                first_seen_at = CASE
                    WHEN first_seen_at IS NULL OR first_seen_at = 'N/A'
                        THEN COALESCE(NULLIF(attempted_at, 'N/A'), NULLIF(sent_at, 'N/A'), ?)
                    ELSE first_seen_at
                END,
                last_seen_at = ?,
                seen_count = CASE
                    WHEN seen_count IS NULL OR seen_count < 1 THEN 2
                    ELSE seen_count + 1
                END,
                last_scan_run_id = ?,
                last_error_message = ?
            WHERE signal_id = ?
              AND alert_type = ?
              AND telegram_status = ?
              AND blocked_reason = ?
              AND error_message = ?
            """,
            (
                now,
                now,
                now,
                record.last_scan_run_id,
                _text(record.last_error_message),
                _identity(record.signal_id),
                _text(record.alert_type),
                status,
                _text(record.blocked_reason),
                _text(record.error_message),
            ),
        )
        return cursor.rowcount > 0

    def compact_existing_attempt(
        self,
        *,
        signal_id: str,
        alert_type: TelegramAlertType | str,
        scan_run_id: str | None = None,
    ) -> TelegramAlertAttemptRecord | None:
        existing = self.get_attempt(signal_id=signal_id, alert_type=alert_type)
        if existing is None or existing.telegram_status not in {"blocked", "skipped"}:
            return existing
        now = now_utc_iso()
        record = TelegramAlertAttemptRecord(
            signal_id=existing.signal_id,
            symbol=existing.symbol,
            direction=existing.direction,
            previous_state=existing.previous_state,
            new_state=existing.new_state,
            alert_type=existing.alert_type,
            lifecycle_state=existing.lifecycle_state,
            sent_at=existing.sent_at,
            attempted_at=existing.attempted_at,
            telegram_status=existing.telegram_status,
            message_hash=existing.message_hash,
            scan_run_id=existing.scan_run_id,
            attempted_alert_type=existing.attempted_alert_type,
            setup_quality_score=existing.setup_quality_score,
            rr_planned=existing.rr_planned,
            min_rr=existing.min_rr,
            opportunity_score=existing.opportunity_score,
            min_score_for_idea=existing.min_score_for_idea,
            technical_score=existing.technical_score,
            price_level=existing.price_level,
            entry_low=existing.entry_low,
            entry_high=existing.entry_high,
            stop_loss=existing.stop_loss,
            tp1=existing.tp1,
            tp2=existing.tp2,
            tp3=existing.tp3,
            blocked_reason=existing.blocked_reason,
            invalid_target_fields=existing.invalid_target_fields,
            error_message=existing.error_message,
            first_seen_at=existing.first_seen_at,
            last_seen_at=now,
            seen_count=existing.seen_count,
            last_scan_run_id=scan_run_id,
            last_error_message=existing.error_message,
            public_watchlist_plan_id=existing.public_watchlist_plan_id,
            public_watchlist_event_key=existing.public_watchlist_event_key,
            public_alert_event_type=existing.public_alert_event_type,
            normalized_entry_zone_low=existing.normalized_entry_zone_low,
            normalized_entry_zone_high=existing.normalized_entry_zone_high,
            normalized_invalidation=existing.normalized_invalidation,
            dedupe_status=existing.dedupe_status,
            dedupe_reason=existing.dedupe_reason,
            id=existing.id,
        )
        self.compact_repeated_attempt(record)
        return existing

    def list_attempts(self, *, signal_id: str | None = None) -> tuple[TelegramAlertAttemptRecord, ...]:
        params: list[Any] = []
        where = ""
        if signal_id is not None:
            where = "WHERE signal_id = ?"
            params.append(_identity(signal_id))
        rows = self._connection.execute(
            f"""
            SELECT * FROM telegram_alert_attempts
            {where}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    @property
    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise StorageError("Telegram alert attempt repository is not open.")
        return self.connection

def _public_alert_event_from_row(row: sqlite3.Row | None) -> PublicAlertEventRecord | None:
    if row is None:
        return None
    return PublicAlertEventRecord(**{key: row[key] for key in row.keys()})


def _public_alert_event_status(value: Any) -> str:
    status = _status_key(value).upper()
    if status in {
        PUBLIC_ALERT_EVENT_RESERVED_STATUS,
        PUBLIC_ALERT_EVENT_SENT_STATUS,
        PUBLIC_ALERT_EVENT_FAILED_STATUS,
        PUBLIC_ALERT_EVENT_BLOCKED_STATUS,
    }:
        return status
    return PUBLIC_ALERT_EVENT_BLOCKED_STATUS


def _get_public_alert_event(
    db: SQLiteTelegramAlertAttemptRepository,
    *,
    event_key: str,
    statuses: Sequence[str] | None = None,
) -> PublicAlertEventRecord | None:
    normalized_event_key = _text(event_key)
    if normalized_event_key == NA:
        return None
    params: list[Any] = [normalized_event_key]
    status_clause = str()
    if statuses:
        normalized_statuses = tuple(_public_alert_event_status(status) for status in statuses if _text(status) != NA)
        if normalized_statuses:
            placeholders = ",".join("?" for _ in normalized_statuses)
            status_clause = f"AND status IN ({placeholders})"
            params.extend(normalized_statuses)
    row = db._connection.execute(
        f"""
        SELECT * FROM public_alert_events
        WHERE event_key = ?
          {status_clause}
        ORDER BY id ASC
        LIMIT 1
        """
,
        params,
    ).fetchone()
    return _public_alert_event_from_row(row)


def _public_alert_event_matches_plan(
    event: PublicAlertEventRecord,
    plan: PublicWatchlistPlanIdentity,
) -> tuple[bool, bool]:
    record = TelegramAlertAttemptRecord(
        signal_id=NA,
        symbol=event.symbol,
        direction=event.side,
        previous_state=NA,
        new_state=NA,
        alert_type=TelegramAlertType.WATCHLIST.value,
        lifecycle_state=NA,
        sent_at=event.sent_at,
        telegram_status=_text(event.status).lower(),
        message_hash=NA,
        entry_low=event.raw_entry_low,
        entry_high=event.raw_entry_high,
        stop_loss=event.raw_stop_loss,
        public_watchlist_plan_id=event.canonical_plan_id,
    )
    if _text(event.setup_family) != NA and plan.setup_family != NA and _text(event.setup_family) != plan.setup_family:
        return False, False
    return _public_watchlist_legacy_record_matches_plan(record, plan)


def _find_matching_public_alert_event(
    db: SQLiteTelegramAlertAttemptRepository,
    *,
    symbol: str,
    direction: Any,
    plan: PublicWatchlistPlanIdentity,
    event_type: str,
) -> tuple[PublicAlertEventRecord | None, bool]:
    normalized_symbol = _symbol(symbol)
    normalized_direction = _status_key(direction)
    normalized_event_type = _status_key(event_type) or PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE
    if normalized_symbol == NA or normalized_direction not in {"long", "short"}:
        return None, False
    statuses = (
        PUBLIC_ALERT_EVENT_SENT_STATUS,
        PUBLIC_ALERT_EVENT_RESERVED_STATUS,
        PUBLIC_ALERT_EVENT_FAILED_STATUS,
    )
    placeholders = ",".join("?" for _ in statuses)
    rows = db._connection.execute(
        f"""
        SELECT * FROM public_alert_events
        WHERE symbol = ?
          AND side = ?
          AND event_type = ?
          AND status IN ({placeholders})
        ORDER BY id ASC
        """
,
        (normalized_symbol, normalized_direction, normalized_event_type, *statuses),
    ).fetchall()
    for row in rows:
        event = _public_alert_event_from_row(row)
        if event is None:
            continue
        matched, jitter = _public_alert_event_matches_plan(event, plan)
        if matched:
            return event, jitter
    return None, False

def _insert_public_alert_event(
    db: SQLiteTelegramAlertAttemptRepository,
    *,
    plan: PublicWatchlistPlanIdentity,
    event_key: str,
    event_type: str,
    status: str,
    reserved_at: str,
    sent_at: str | None = None,
    matched_prior_alert_id: int | None = None,
    matched_prior_event_id: int | None = None,
    failure_reason: str = NA,
) -> tuple[PublicAlertEventRecord | None, bool]:
    normalized_event_key = _text(event_key)
    if normalized_event_key == NA:
        return None, False
    source_modes = ",".join(plan.source_modes) if plan.source_modes else NA
    ledger_status = _public_alert_event_status(status)
    timestamp = reserved_at if _text(reserved_at) != NA else now_utc_iso()
    try:
        db._connection.execute(
            """
            INSERT INTO public_alert_events (
                canonical_plan_id, event_type, event_key, symbol, side, setup_family,
                normalized_zone_low, normalized_zone_high, normalized_invalidation,
                raw_entry_low, raw_entry_high, raw_stop_loss, status, reserved_at, sent_at,
                source_modes, matched_prior_alert_id, matched_prior_event_id, failure_reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
,
            (
                _text(plan.plan_id),
                _status_key(event_type) or PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
                normalized_event_key,
                _symbol(plan.symbol),
                _status_key(plan.side),
                _text(plan.setup_family),
                _text(plan.normalized_entry_low),
                _text(plan.normalized_entry_high),
                _text(plan.normalized_invalidation),
                _text(plan.raw_entry_low),
                _text(plan.raw_entry_high),
                _text(plan.raw_invalidation),
                ledger_status,
                timestamp,
                sent_at if _text(sent_at) != NA else None,
                source_modes,
                int(matched_prior_alert_id) if matched_prior_alert_id is not None else None,
                int(matched_prior_event_id) if matched_prior_event_id is not None else None,
                _text(failure_reason),
                timestamp,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError:
        return _get_public_alert_event(db, event_key=normalized_event_key), False
    return _get_public_alert_event(db, event_key=normalized_event_key), True


def _mark_public_alert_event_result(
    db: SQLiteTelegramAlertAttemptRepository,
    *,
    event_id: int,
    status: str,
    sent_at: str | None,
    failure_reason: str,
) -> bool:
    ledger_status = PUBLIC_ALERT_EVENT_SENT_STATUS if _text(status) == "sent" else PUBLIC_ALERT_EVENT_FAILED_STATUS
    now = now_utc_iso()
    cursor = db._connection.execute(
        """
        UPDATE public_alert_events
        SET status = ?,
            sent_at = ?,
            failure_reason = ?,
            updated_at = ?
        WHERE id = ?
        """
,
        (
            ledger_status,
            sent_at if ledger_status == PUBLIC_ALERT_EVENT_SENT_STATUS else None,
            NA if ledger_status == PUBLIC_ALERT_EVENT_SENT_STATUS else _text(failure_reason),
            now,
            int(event_id),
        ),
    )
    return cursor.rowcount > 0


def _reserve_existing_public_alert_event(
    db: SQLiteTelegramAlertAttemptRepository,
    *,
    event_id: int,
    reserved_at: str,
) -> bool:
    timestamp = reserved_at if _text(reserved_at) != NA else now_utc_iso()
    cursor = db._connection.execute(
        """
        UPDATE public_alert_events
        SET status = ?,
            reserved_at = ?,
            sent_at = NULL,
            failure_reason = ?,
            updated_at = ?
        WHERE id = ?
          AND status = ?
          AND delivery_state = ?
        """,
        (
            PUBLIC_ALERT_EVENT_RESERVED_STATUS,
            timestamp,
            NA,
            timestamp,
            int(event_id),
            PUBLIC_ALERT_EVENT_FAILED_STATUS,
            RETRYABLE,
        ),
    )
    return cursor.rowcount > 0


def _block_duplicate_public_alert_event(
    db: SQLiteTelegramAlertAttemptRepository,
    *,
    plan: PublicWatchlistPlanIdentity,
    event_key: str,
    event_type: str,
    attempted_at: str,
    matched_prior_alert_id: int | None = None,
    matched_prior_event_id: int | None = None,
    reason: str = PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON,
) -> PublicAlertEventRecord | None:
    event, _ = _insert_public_alert_event(
        db,
        plan=plan,
        event_key=event_key,
        event_type=event_type,
        status=PUBLIC_ALERT_EVENT_BLOCKED_STATUS,
        reserved_at=attempted_at,
        matched_prior_alert_id=matched_prior_alert_id,
        matched_prior_event_id=matched_prior_event_id,
        failure_reason=reason,
    )
    return event


def reserve_public_watchlist_event(
    db: SQLiteTelegramAlertAttemptRepository,
    *,
    symbol: str,
    side: str,
    canonical_plan_id: str,
    event_type: str,
    payload: Mapping[str, Any],
) -> PublicWatchlistReservationResult:
    event_key = _public_watchlist_event_key(canonical_plan_id, event_type)
    if event_key == NA:
        return PublicWatchlistReservationResult(
            granted=False,
            event_key=NA,
            status="blocked",
            reason="missing_public_watchlist_event_key",
            detail="Public watchlist event key could not be built.",
        )

    reservation_record = payload.get("reservation_record")
    if not isinstance(reservation_record, TelegramAlertAttemptRecord):
        return PublicWatchlistReservationResult(
            granted=False,
            event_key=event_key,
            status="blocked",
            reason="missing_public_watchlist_reservation_record",
            detail="Public watchlist reservation payload was incomplete.",
        )
    plan = payload.get("canonical_plan")
    if not isinstance(plan, PublicWatchlistPlanIdentity):
        plan = PublicWatchlistPlanIdentity(plan_id=canonical_plan_id, symbol=_symbol(symbol), side=_status_key(side))
    active_event_statuses = (
        PUBLIC_ALERT_EVENT_SENT_STATUS,
        PUBLIC_ALERT_EVENT_RESERVED_STATUS,
        PUBLIC_ALERT_EVENT_FAILED_STATUS,
    )
    prior_event = _get_public_alert_event(event_key=event_key, statuses=active_event_statuses, db=db)
    event_jitter_matched = False
    if prior_event is None:
        prior_event, event_jitter_matched = _find_matching_public_alert_event(
            db,
            symbol=symbol,
            direction=side,
            plan=plan,
            event_type=event_type,
        )
    if prior_event is not None:
        prior_attempt = db.find_successful_public_watchlist_event(event_key=prior_event.event_key)
        delivery_state = _text(prior_event.delivery_state).upper()
        if delivery_state in {PENDING, RETRYABLE, IN_FLIGHT}:
            active_attempt = db.get_public_watchlist_event_attempt(event_key=prior_event.event_key)
            if active_attempt is not None and active_attempt.id is not None:
                return PublicWatchlistReservationResult(
                    granted=True,
                    event_key=prior_event.event_key,
                    reservation_id=active_attempt.id,
                    event_id=prior_event.id,
                    status=delivery_state.lower(),
                    reason=NA,
                    detail="Committed public watchlist delivery intent is available for atomic claim.",
                    matched_prior_event_id=prior_event.id,
                )
        if delivery_state == UNCERTAIN:
            return PublicWatchlistReservationResult(
                granted=False,
                event_key=prior_event.event_key,
                event_id=prior_event.id,
                status="uncertain",
                reason="public_watchlist_delivery_uncertain",
                detail="Prior Telegram acceptance is uncertain; automatic resend is prohibited.",
            )
        if prior_event.status == PUBLIC_ALERT_EVENT_SENT_STATUS:
            if prior_attempt is not None:
                _persist_public_watchlist_duplicate_reservation_skip(
                    db,
                    reservation_record,
                    matched_prior=prior_attempt,
                    fallback_matched_legacy_sent=False,
                    jitter_matched_same_plan=event_jitter_matched,
                    matched_prior_event_id=prior_event.id,
                )
            return PublicWatchlistReservationResult(
                granted=False,
                event_key=event_key,
                status="skipped",
                reason=PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON,
                detail=_public_watchlist_reservation_duplicate_detail(
                    fallback_matched_legacy_sent=False,
                    jitter_matched_same_plan=event_jitter_matched,
                ),
                matched_prior_alert_id=prior_attempt.id if prior_attempt is not None else None,
                matched_prior_event_id=prior_event.id,
                fallback_matched_legacy_sent=False,
                jitter_matched_same_plan=event_jitter_matched,
            )
        return PublicWatchlistReservationResult(
            granted=False,
            event_key=event_key,
            event_id=prior_event.id,
            status="skipped",
            reason=PUBLIC_WATCHLIST_RESERVATION_IN_FLIGHT_REASON,
            detail="Public watchlist event is already reserved by another send path.",
            matched_prior_event_id=prior_event.id,
        )


    prior = db.find_successful_public_watchlist_event(event_key=event_key)
    fallback_matched = False
    jitter_matched = False
    if prior is None:
        prior, fallback_matched, jitter_matched = db.find_matching_successful_public_watchlist_event(
            symbol=symbol,
            direction=side,
            plan=plan,
            event_type=event_type,
            since=payload.get("legacy_since"),
        )
    if prior is not None:
        blocked_event = _block_duplicate_public_alert_event(
            db,
            plan=plan,
            event_key=event_key,
            event_type=event_type,
            attempted_at=_text(reservation_record.attempted_at),
            matched_prior_alert_id=prior.id,
            reason=PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON,
        )
        _persist_public_watchlist_duplicate_reservation_skip(
            db,
            reservation_record,
            matched_prior=prior,
            fallback_matched_legacy_sent=fallback_matched,
            jitter_matched_same_plan=jitter_matched,
            matched_prior_event_id=blocked_event.id if blocked_event is not None else None,
        )
        return PublicWatchlistReservationResult(
            granted=False,
            event_key=event_key,
            status="skipped",
            reason=PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON,
            detail=_public_watchlist_reservation_duplicate_detail(
                fallback_matched_legacy_sent=fallback_matched,
                jitter_matched_same_plan=jitter_matched,
            ),
            matched_prior_alert_id=prior.id,
            fallback_matched_legacy_sent=fallback_matched,
            jitter_matched_same_plan=jitter_matched,
        )

    existing = db.get_attempt(signal_id=reservation_record.signal_id, alert_type=TelegramAlertType.WATCHLIST)
    if existing is not None:
        if existing.telegram_status == "sent":
            _persist_public_watchlist_duplicate_reservation_skip(
                db,
                reservation_record,
                matched_prior=existing,
                fallback_matched_legacy_sent=False,
                jitter_matched_same_plan=False,
            )
            return PublicWatchlistReservationResult(
                granted=False,
                event_key=event_key,
                status="skipped",
                reason=PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON,
                detail="Prior successful public watchlist signal already exists.",
                matched_prior_alert_id=existing.id,
            )
        if existing.telegram_status == PUBLIC_WATCHLIST_RESERVATION_STATUS:
            return PublicWatchlistReservationResult(
                granted=False,
                event_key=event_key,
                reservation_id=existing.id,
                status="skipped",
                reason=PUBLIC_WATCHLIST_RESERVATION_IN_FLIGHT_REASON,
                detail="Public watchlist event is already reserved by another send path.",
            )
        if existing.id is not None:
            ledger_event, ledger_inserted = _insert_public_alert_event(
                db,
                plan=plan,
                event_key=event_key,
                event_type=event_type,
                status=PUBLIC_ALERT_EVENT_RESERVED_STATUS,
                reserved_at=_text(reservation_record.attempted_at),
            )
            if ledger_event is None:
                return PublicWatchlistReservationResult(
                    granted=False,
                    event_key=event_key,
                    status="blocked",
                    reason="public_alert_event_reservation_failed",
                    detail="Public alert event ledger reservation could not be created.",
                )
            if not ledger_inserted:
                retry_reserved = (
                    ledger_event.status == PUBLIC_ALERT_EVENT_FAILED_STATUS
                    and ledger_event.id is not None
                    and _reserve_existing_public_alert_event(
                        db,
                        event_id=ledger_event.id,
                        reserved_at=_text(reservation_record.attempted_at),
                    )
                )
                if not retry_reserved:
                    return PublicWatchlistReservationResult(
                        granted=False,
                        event_key=event_key,
                        event_id=ledger_event.id,
                        status="skipped",
                        reason=PUBLIC_WATCHLIST_RESERVATION_IN_FLIGHT_REASON,
                        detail="Public watchlist event is already reserved by another send path.",
                        matched_prior_event_id=ledger_event.id,
                    )
            if db.replace_attempt_with_reservation(attempt_id=existing.id, record=reservation_record):
                return PublicWatchlistReservationResult(
                    granted=True,
                    event_key=event_key,
                    reservation_id=existing.id,
                    event_id=ledger_event.id,
                    status=PUBLIC_WATCHLIST_RESERVATION_STATUS,
                    reason=NA,
                    detail="Public watchlist event reservation refreshed for retry.",
                )

    ledger_event, ledger_inserted = _insert_public_alert_event(
        db,
        plan=plan,
        event_key=event_key,
        event_type=event_type,
        status=PUBLIC_ALERT_EVENT_RESERVED_STATUS,
        reserved_at=_text(reservation_record.attempted_at),
    )
    if ledger_event is None:
        return PublicWatchlistReservationResult(
            granted=False,
            event_key=event_key,
            status="blocked",
            reason="public_alert_event_reservation_failed",
            detail="Public alert event ledger reservation could not be created.",
        )
    if not ledger_inserted:
        prior_attempt = db.find_successful_public_watchlist_event(event_key=ledger_event.event_key)
        if ledger_event.status == PUBLIC_ALERT_EVENT_SENT_STATUS:
            if prior_attempt is not None:
                _persist_public_watchlist_duplicate_reservation_skip(
                    db,
                    reservation_record,
                    matched_prior=prior_attempt,
                    fallback_matched_legacy_sent=False,
                    jitter_matched_same_plan=False,
                    matched_prior_event_id=ledger_event.id,
                )
            return PublicWatchlistReservationResult(
                granted=False,
                event_key=event_key,
                event_id=ledger_event.id,
                status="skipped",
                reason=PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON,
                detail="Prior successful public watchlist event already exists.",
                matched_prior_alert_id=prior_attempt.id if prior_attempt is not None else None,
                matched_prior_event_id=ledger_event.id,
            )
        retry_reserved = (
            ledger_event.status == PUBLIC_ALERT_EVENT_FAILED_STATUS
            and ledger_event.id is not None
            and _reserve_existing_public_alert_event(
                db,
                event_id=ledger_event.id,
                reserved_at=_text(reservation_record.attempted_at),
            )
        )
        if not retry_reserved:
            return PublicWatchlistReservationResult(
                granted=False,
                event_key=event_key,
                event_id=ledger_event.id,
                status="skipped",
                reason=PUBLIC_WATCHLIST_RESERVATION_IN_FLIGHT_REASON,
                detail="Public watchlist event is already reserved by another send path.",
                matched_prior_event_id=ledger_event.id,
            )

    inserted = db.insert_attempt(reservation_record)
    if inserted:
        reserved = db.get_attempt(signal_id=reservation_record.signal_id, alert_type=TelegramAlertType.WATCHLIST)
        return PublicWatchlistReservationResult(
            granted=True,
            event_key=event_key,
            reservation_id=reserved.id if reserved is not None else None,
            event_id=ledger_event.id,
            status=PUBLIC_WATCHLIST_RESERVATION_STATUS,
            reason=NA,
            detail="Public watchlist event reserved before send.",
        )

    active = db.get_public_watchlist_event_attempt(
        event_key=event_key,
        statuses=(PUBLIC_WATCHLIST_RESERVATION_STATUS, "sent"),
    )
    if active is not None and active.telegram_status == "sent":
        _persist_public_watchlist_duplicate_reservation_skip(
            db,
            reservation_record,
            matched_prior=active,
            fallback_matched_legacy_sent=False,
            jitter_matched_same_plan=False,
        )
        return PublicWatchlistReservationResult(
            granted=False,
            event_key=event_key,
            status="skipped",
            reason=PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON,
            detail="Prior successful public watchlist event already exists.",
            matched_prior_alert_id=active.id,
        )
    if active is not None:
        return PublicWatchlistReservationResult(
            granted=False,
            event_key=event_key,
            reservation_id=active.id,
            status="skipped",
            reason=PUBLIC_WATCHLIST_RESERVATION_IN_FLIGHT_REASON,
            detail="Public watchlist event is already reserved by another send path.",
        )

    retry_existing = db.get_attempt(signal_id=reservation_record.signal_id, alert_type=TelegramAlertType.WATCHLIST)
    if retry_existing is not None and retry_existing.id is not None:
        if db.replace_attempt_with_reservation(attempt_id=retry_existing.id, record=reservation_record):
            return PublicWatchlistReservationResult(
                granted=True,
                event_key=event_key,
                reservation_id=retry_existing.id,
                event_id=ledger_event.id,
                status=PUBLIC_WATCHLIST_RESERVATION_STATUS,
                reason=NA,
                detail="Public watchlist event reservation refreshed for retry.",
            )

    return PublicWatchlistReservationResult(
        granted=False,
        event_key=event_key,
        status="duplicate",
        reason=PUBLIC_WATCHLIST_RESERVATION_IN_FLIGHT_REASON,
        detail="Public watchlist event reservation could not be acquired.",
    )


def _public_watchlist_reservation_duplicate_detail(
    *,
    fallback_matched_legacy_sent: bool,
    jitter_matched_same_plan: bool,
) -> str:
    parts = ["Prior successful public watchlist event already exists.", PUBLIC_WATCHLIST_DUPLICATE_EQUIVALENT_PLAN_REASON]
    if fallback_matched_legacy_sent:
        parts.append("fallback_matched_legacy_sent")
    if jitter_matched_same_plan:
        parts.append("jitter_matched_same_plan")
    return ";".join(parts)


def _persist_public_watchlist_duplicate_reservation_skip(
    repository: SQLiteTelegramAlertAttemptRepository,
    reservation_record: TelegramAlertAttemptRecord,
    *,
    matched_prior: TelegramAlertAttemptRecord | None,
    fallback_matched_legacy_sent: bool,
    jitter_matched_same_plan: bool,
    matched_prior_event_id: int | None = None,
) -> None:
    reason = PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON
    detail_parts = [reason, PUBLIC_WATCHLIST_DUPLICATE_EQUIVALENT_PLAN_REASON]
    if matched_prior is not None and matched_prior.id is not None:
        detail_parts.append(f"matched_prior_sent_row_id={matched_prior.id}")
    if matched_prior_event_id is not None:
        detail_parts.append(f"matched_prior_event_id={matched_prior_event_id}")
    if fallback_matched_legacy_sent:
        detail_parts.append("fallback_matched_legacy_sent")
    if jitter_matched_same_plan:
        detail_parts.append("jitter_matched_same_plan")
    skip_record = replace(
        reservation_record,
        alert_type=_blocked_alert_type(TelegramAlertType.WATCHLIST, reason),
        sent_at=None,
        telegram_status="skipped",
        blocked_reason=reason,
        error_message=reason,
        dedupe_status="skipped",
        dedupe_reason=";".join(detail_parts),
        last_error_message=reason,
    )
    inserted = repository.insert_attempt(skip_record)
    if not inserted:
        repository.compact_repeated_attempt(skip_record)
    logger.info(
        "simple_signal_duplicate_blocked symbol=%s bias=%s signature=%s cooldown_remaining=%s",
        skip_record.symbol,
        skip_record.direction,
        skip_record.public_watchlist_plan_id,
        NA,
    )
    logger.info(
        "public_watchlist_duplicate_skipped symbol=%s side=%s raw_zone=%s-%s raw_invalidation=%s "
        "normalized_zone=%s-%s normalized_invalidation=%s matched_prior_sent_row_id=%s "
        "canonical_plan_id=%s event_type=%s dedupe_reason=%s",
        skip_record.symbol,
        skip_record.direction,
        skip_record.entry_low,
        skip_record.entry_high,
        skip_record.stop_loss,
        skip_record.normalized_entry_zone_low,
        skip_record.normalized_entry_zone_high,
        skip_record.normalized_invalidation,
        matched_prior.id if matched_prior is not None and matched_prior.id is not None else NA,
        skip_record.public_watchlist_plan_id,
        skip_record.public_alert_event_type,
        skip_record.dedupe_reason,
    )


def _persist_public_watchlist_final_send_guard_skip(
    repository: SQLiteTelegramAlertAttemptRepository,
    symbol_result: ScannerSymbolResult,
    *,
    decision: TelegramAlertDecision,
    reservation: PublicWatchlistReservationResult | None,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> TelegramLifecycleDelivery:
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert decision.message is not None
    reason = _public_watchlist_final_send_guard_reason(reservation)
    _record_public_watchlist_final_send_guard_event(
        repository,
        symbol_result=symbol_result,
        message=decision.message,
        reservation=reservation,
        reason=reason,
    )
    return _persist_skipped_public_watchlist_attempt(
        repository,
        symbol_result,
        decision=replace(decision, eligible=False, reason=reason),
        reason=reason,
        scan_run_id=scan_run_id,
        eligibility_context=eligibility_context,
    )


def _public_watchlist_final_send_guard_reason(reservation: PublicWatchlistReservationResult | None) -> str:
    if reservation is None:
        return PUBLIC_WATCHLIST_MISSING_REQUIRED_RESERVATION_REASON
    if not reservation.granted and reservation.reason == PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON:
        return PUBLIC_WATCHLIST_DUPLICATE_EQUIVALENT_PLAN_REASON
    if reservation.reservation_id is None or reservation.event_id is None:
        return PUBLIC_WATCHLIST_MISSING_REQUIRED_RESERVATION_REASON
    return _first_non_na(reservation.reason, PUBLIC_WATCHLIST_MISSING_REQUIRED_RESERVATION_REASON)


def _record_public_watchlist_final_send_guard_event(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    reservation: PublicWatchlistReservationResult | None,
    reason: str,
) -> None:
    plan = _public_watchlist_canonical_plan(symbol_result, message)
    event_key = _public_watchlist_event_key(plan.plan_id, PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE)
    if plan.plan_id == NA or event_key == NA:
        return
    _block_duplicate_public_alert_event(
        repository,
        plan=plan,
        event_key=event_key,
        event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
        attempted_at=now_utc_iso(),
        matched_prior_alert_id=reservation.matched_prior_alert_id if reservation is not None else None,
        matched_prior_event_id=reservation.matched_prior_event_id if reservation is not None else None,
        reason=reason,
    )


async def _deliver_committed_public_watchlist_intent(
    *,
    repository: SQLiteTelegramAlertAttemptRepository,
    sender: Any,
    reservation: PublicWatchlistReservationResult,
    symbol_result: ScannerSymbolResult,
    signal_id: str,
    message: TelegramSignalMessage,
    message_text: str,
    message_hash: str,
    attempted_at: str,
) -> TelegramLifecycleDelivery:
    """Commit intent, atomically claim it, then durably send one part at a time."""

    assert reservation.event_id is not None
    assert reservation.reservation_id is not None
    event_id = reservation.event_id
    reservation_id = reservation.reservation_id
    connection = repository._connection
    try:
        existing_part_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM public_alert_delivery_parts WHERE public_alert_event_id = ?",
                (event_id,),
            ).fetchone()[0]
        )
        if existing_part_count == 0:
            parts = persist_intent_parts(
                connection,
                event_id=event_id,
                event_key=reservation.event_key,
                message_text=message_text,
                message_hash=message_hash,
                destination_chat_id=getattr(sender, "destination_identity", NA),
                destination_kind=getattr(sender, "destination_kind", NA),
            )
            connection.execute(
                """
                UPDATE telegram_alert_attempts
                SET telegram_status = 'pending', delivery_state = ?,
                    delivery_part_count = ?, dedupe_status = 'pending',
                    blocked_reason = ?, error_message = ?, last_error_message = ?
                WHERE id = ?
                """,
                (PENDING, len(parts), NA, NA, NA, reservation_id),
            )
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise StorageError("Unable to commit Telegram delivery intent before network activity.") from exc

    outbox = SQLitePublicTelegramOutbox(connection)
    message_type = _telegram_message_type_for_alert(TelegramAlertType.WATCHLIST, message)
    preflight_guard = PublicWatchlistSendGuard(
        event_key=reservation.event_key,
        reservation_id=reservation_id,
        event_id=event_id,
        attempt_id="committed-intent-preflight",
    )
    if bool(getattr(sender, "dry_run", False)):
        outbox.mark_terminal_without_send(
            event_id=event_id,
            reservation_id=reservation_id,
            state=SKIPPED_DRY_RUN,
            reason="telegram_dry_run_enabled",
        )
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=TelegramAlertType.WATCHLIST.value,
            status="skipped",
            detail="Telegram dry-run evaluated the committed intent with zero HTTP activity.",
            message_hash=message_hash,
            error_message="telegram_dry_run_enabled",
        )
    preflight = getattr(sender, "preflight", None)
    if callable(preflight):
        blocked = preflight(message_type=message_type, public_watchlist_guard=preflight_guard)
        if blocked is not None:
            state = _text(getattr(blocked, "delivery_state", FAILED_FINAL)).upper()
            terminal_state = POLICY_DISABLED if state == POLICY_DISABLED else FAILED_FINAL
            outbox.mark_terminal_without_send(
                event_id=event_id,
                reservation_id=reservation_id,
                state=terminal_state,
                reason=_first_non_na(blocked.error_message, blocked.detail),
            )
            return TelegramLifecycleDelivery(
                symbol=symbol_result.symbol,
                signal_id=signal_id,
                alert_type=TelegramAlertType.WATCHLIST.value,
                status="skipped" if terminal_state == POLICY_DISABLED else "failed",
                detail=blocked.detail,
                message_hash=message_hash,
                error_message=blocked.error_message,
            )

    claim_result = outbox.claim(
        event_id=event_id,
        reservation_id=reservation_id,
        now=now_utc_iso(),
    )
    if claim_result.claim is None:
        state = claim_result.state
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=TelegramAlertType.WATCHLIST.value,
            status=(
                "duplicate" if state == SENT
                else "uncertain" if state == UNCERTAIN
                else "failed" if state == FAILED_FINAL
                else "skipped"
            ),
            detail=f"Telegram outbox claim not granted: {claim_result.reason}.",
            message_hash=message_hash,
            error_message=claim_result.reason,
        )

    claim = claim_result.claim
    guard = PublicWatchlistSendGuard(
        event_key=claim.event_key,
        reservation_id=claim.reservation_id,
        event_id=claim.event_id,
        attempt_id=claim.attempt_id,
    )
    for part in claim.parts:
        if not outbox.mark_part_in_flight(part_id=part.id, attempt_id=claim.attempt_id):
            outbox.mark_uncertain_after_persistence_failure(
                event_id=event_id,
                reservation_id=reservation_id,
                detail="Telegram part could not enter IN_FLIGHT before HTTP.",
            )
            return TelegramLifecycleDelivery(
                symbol=symbol_result.symbol,
                signal_id=signal_id,
                alert_type=TelegramAlertType.WATCHLIST.value,
                status="uncertain",
                detail="Telegram part claim became uncertain before transport.",
                message_hash=message_hash,
                error_message="part_in_flight_commit_failed",
            )
        repository.commit_before_network_activity()
        try:
            send_part = getattr(sender, "send_part", None)
            if callable(send_part):
                send_result = await send_part(
                    part.payload_text,
                    part_number=part.part_index,
                    total_parts=part.part_count,
                    message_type=message_type,
                    public_watchlist_guard=guard,
                )
            else:
                send_result = await sender.send_text(
                    part.payload_text,
                    message_type=message_type,
                    public_watchlist_guard=guard,
                )
        except Exception as exc:
            send_result = None
            transport_result: dict[str, Any] = {
                "delivery_state": UNCERTAIN,
                "error_category": "sender_exception_after_attempt_started",
                "error": f"{type(exc).__name__}: sender raised after the attempt started",
            }
        else:
            transport_result = _telegram_transport_result(send_result)
        try:
            state = outbox.record_part_result(
                event_id=event_id,
                reservation_id=reservation_id,
                part_id=part.id,
                attempt_id=claim.attempt_id,
                result=transport_result,
            )
        except sqlite3.Error as exc:
            outbox.mark_uncertain_after_persistence_failure(
                event_id=event_id,
                reservation_id=reservation_id,
                detail=f"{type(exc).__name__}: confirmed result persistence failed",
            )
            state = UNCERTAIN
        if state == IN_FLIGHT:
            continue
        if state == SENT:
            return TelegramLifecycleDelivery(
                symbol=symbol_result.symbol, signal_id=signal_id,
                alert_type=TelegramAlertType.WATCHLIST.value, status="sent",
                detail="Telegram setup delivery confirmed and persisted.",
                message_hash=message_hash,
            )
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=TelegramAlertType.WATCHLIST.value,
            status="failed" if state in {RETRYABLE, FAILED_FINAL} else "uncertain",
            detail=f"Telegram setup delivery ended in {state}.",
            message_hash=message_hash,
            error_message=_first_non_na(
                transport_result.get("error_category"), transport_result.get("error")
            ),
        )

    return TelegramLifecycleDelivery(
        symbol=symbol_result.symbol, signal_id=signal_id,
        alert_type=TelegramAlertType.WATCHLIST.value, status="uncertain",
        detail="Telegram multipart delivery ended without a durable terminal result.",
        message_hash=message_hash, error_message="multipart_terminal_state_missing",
    )


def _telegram_transport_result(send_result: Any) -> dict[str, Any]:
    results = tuple(getattr(send_result, "telegram_results", ()) or ())
    if results:
        return dict(results[0])
    declared_state = _text(getattr(send_result, "delivery_state", NA)).upper()
    if declared_state == NA:
        declared_state = SENT if getattr(send_result, "status", None) == "sent" else UNCERTAIN
    state = UNCERTAIN if declared_state == SENT else declared_state
    return {
        "delivery_state": state,
        "error_category": (
            "missing_confirmed_transport_metadata"
            if declared_state == SENT
            else "sender_result_unclassified"
        ),
        "error": _first_non_na(
            getattr(send_result, "error_message", NA), getattr(send_result, "detail", NA)
        ),
    }


class TelegramLifecycleDeliveryService:
    def __init__(
        self,
        *,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
        settings: Settings | None = None,
        sender: TelegramSender | None = None,
        min_rr: Decimal | str | None = None,
        min_score_for_idea: Decimal | str | None = None,
        min_technical_score: Decimal | str = DEFAULT_MIN_TECHNICAL_SCORE,
    ) -> None:
        self.database_path = Path(database_path)
        self.settings = settings or Settings()
        self.sender = sender or TelegramSender.from_settings(self.settings)
        self.min_rr = _decimal_or_default(min_rr, DEFAULT_CONFIRMED_MIN_RR)
        self.min_score_for_idea = _decimal_or_none(min_score_for_idea)
        self.min_technical_score = _decimal_or_default(min_technical_score, DEFAULT_MIN_TECHNICAL_SCORE)

    @property
    def watchlist_outcome_tracking_enabled(self) -> bool:
        return bool(getattr(self.settings, "telegram_watchlist_outcome_tracking_enabled", True))

    @property
    def public_watchlist_terminal_updates_enabled(self) -> bool:
        return bool(getattr(self.settings, "telegram_public_watchlist_terminal_updates_enabled", False))

    @property
    def public_watchlist_enabled(self) -> bool:
        return bool(getattr(self.settings, "telegram_public_watchlist_enabled", True))

    @property
    def public_signal_policy(self) -> str:
        configured = _status_key(
            getattr(
                self.settings,
                "telegram_public_signal_policy",
                PUBLIC_SIGNAL_POLICY_SETUP_ONLY,
            )
        )
        if configured != PUBLIC_SIGNAL_POLICY_SETUP_ONLY:
            logger.warning("Unsupported public signal policy ignored; enforcing setup_only.")
            return PUBLIC_SIGNAL_POLICY_SETUP_ONLY
        return configured

    @property
    def setup_only_public_delivery(self) -> bool:
        return self.public_signal_policy == PUBLIC_SIGNAL_POLICY_SETUP_ONLY

    @property
    def public_watchlist_min_score(self) -> Decimal:
        configured = _decimal_or_default(
            getattr(self.settings, "public_watchlist_min_score", PUBLIC_WATCHLIST_MIN_SCORE),
            PUBLIC_WATCHLIST_MIN_SCORE,
        )
        return max(configured, PUBLIC_SIGNAL_MIN_SCORE)

    @property
    def public_watchlist_min_grade(self) -> str:
        grade = _text(getattr(self.settings, "public_watchlist_min_grade", PUBLIC_WATCHLIST_MIN_GRADE))
        return _stricter_public_min_grade(grade if grade != NA else PUBLIC_WATCHLIST_MIN_GRADE)

    @property
    def public_watchlist_min_rr(self) -> Decimal:
        configured = _decimal_or_default(
            getattr(self.settings, "public_watchlist_min_rr", PUBLIC_WATCHLIST_MIN_RR),
            PUBLIC_WATCHLIST_MIN_RR,
        )
        return max(configured, PUBLIC_SIGNAL_MIN_RR)

    @property
    def public_watchlist_max_per_scan(self) -> int:
        configured = max(0, int(getattr(self.settings, "public_watchlist_max_per_scan", PUBLIC_WATCHLIST_MAX_PER_SCAN)))
        return min(configured, PUBLIC_SIGNAL_MAX_PER_SCAN)

    @property
    def public_watchlist_max_per_24h(self) -> int:
        configured = max(0, int(getattr(self.settings, "public_watchlist_max_per_24h", PUBLIC_WATCHLIST_MAX_PER_24H)))
        return min(configured, PUBLIC_SIGNAL_MAX_PER_24H)

    @property
    def public_watchlist_max_per_60m(self) -> int:
        configured = max(0, int(getattr(self.settings, "public_watchlist_max_per_60m", PUBLIC_WATCHLIST_MAX_PER_60M)))
        return min(configured, PUBLIC_SIGNAL_MAX_PER_60M)

    @property
    def public_watchlist_cooldown_hours(self) -> int:
        return PUBLIC_SIGNAL_SAME_SIDE_COOLDOWN_HOURS

    @property
    def public_watchlist_symbol_whitelist(self) -> tuple[str, ...]:
        return _symbol_whitelist(getattr(self.settings, "public_watchlist_symbol_whitelist", ""))
    @property
    def public_watchlist_dedupe_across_modes(self) -> bool:
        return bool(getattr(self.settings, "public_watchlist_dedupe_across_modes", True))

    @property
    def public_watchlist_require_plan(self) -> bool:
        return bool(getattr(self.settings, "public_watchlist_require_plan", True))

    @property
    def public_watchlist_require_entry_zone(self) -> bool:
        return bool(getattr(self.settings, "public_watchlist_require_entry_zone", True))

    @property
    def public_watchlist_require_invalidation(self) -> bool:
        return bool(getattr(self.settings, "public_watchlist_require_invalidation", True))

    @property
    def research_watch_enabled(self) -> bool:
        return bool(getattr(self.settings, "telegram_research_watch_enabled", False))

    @property
    def research_watch_to_public(self) -> bool:
        return bool(getattr(self.settings, "telegram_research_watch_to_public", False))

    @property
    def research_min_quality(self) -> int:
        return max(0, int(getattr(self.settings, "telegram_research_min_quality", 60)))

    @property
    def research_min_readiness(self) -> int:
        return max(0, int(getattr(self.settings, "telegram_research_min_readiness", 50)))

    @property
    def research_alert_cooldown_minutes(self) -> int:
        return max(0, int(getattr(self.settings, "telegram_research_alert_cooldown_minutes", 1440)))

    @property
    def research_max_per_scan(self) -> int:
        return max(0, int(getattr(self.settings, "telegram_research_max_per_scan", 5)))

    async def deliver_for_run(
        self,
        result: ScannerRunResult,
        *,
        scan_run_id: str | None = None,
    ) -> TelegramLifecycleDeliverySummary:
        deliveries: list[TelegramLifecycleDelivery] = []
        ineligible = 0
        duplicate = 0
        sent = 0
        skipped = 0
        failed = 0
        blocked = 0
        blocked_repeat = 0
        current_run_attempts: set[tuple[str, str]] = set()
        current_run_identity_blocked_symbols: set[str] = set()
        public_watchlist_audits: dict[str, PublicWatchlistCandidateAudit] = {}
        confirmed_candidates_seen = 0
        confirmed_prefilter_passed = 0
        confirmed_policy_disabled = 0
        signal_confirmed_attempts_created = 0
        signal_confirmed_sent = 0
        confirmed_policy_disabled_by_reason: dict[str, int] = {}
        confirmed_blocked_before_attempt: dict[str, int] = {}

        def record_delivery(delivery: TelegramLifecycleDelivery) -> None:
            nonlocal duplicate, sent, skipped, failed, blocked, blocked_repeat
            nonlocal signal_confirmed_attempts_created, signal_confirmed_sent
            deliveries.append(delivery)
            if delivery.status in {"sent", "failed", "duplicate", "blocked", "blocked_repeat", "skipped"}:
                current_run_attempts.add((delivery.signal_id, delivery.alert_type))
            if delivery.status in {"blocked", "blocked_repeat"} and delivery.error_message in TERMINAL_IDENTITY_BLOCK_REASONS:
                current_run_identity_blocked_symbols.add(_symbol(delivery.symbol))
            if delivery.status == "duplicate":
                duplicate += 1
            elif delivery.status == "sent":
                sent += 1
            elif delivery.status == "failed":
                failed += 1
            elif delivery.status == "blocked":
                blocked += 1
            elif delivery.status == "blocked_repeat":
                blocked_repeat += 1
            else:
                skipped += 1
            if (
                not self.setup_only_public_delivery
                and delivery.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
            ):
                if delivery.status in {"sent", "failed", "skipped", "blocked"}:
                    signal_confirmed_attempts_created += 1
                if delivery.status == "sent":
                    signal_confirmed_sent += 1

        with SQLiteTelegramAlertAttemptRepository(self.database_path) as repository:
            with SQLiteSetupLifecycleRepository(self.database_path) as lifecycle_repository:
                prior_sent_watchlists = repository.list_sent_watchlist_alerts()
                min_score_for_idea = self.min_score_for_idea
                if min_score_for_idea is None:
                    min_score_for_idea = _decimal_or_none(result.config.min_score_for_idea)
                eligibility_context = TelegramEligibilityContext(
                    min_rr=self.min_rr,
                    min_score_for_idea=min_score_for_idea,
                    min_technical_score=self.min_technical_score,
                    public_watchlist_enabled=self.public_watchlist_enabled,
                    public_watchlist_min_grade=self.public_watchlist_min_grade,
                    public_watchlist_min_score=self.public_watchlist_min_score,
                    public_watchlist_min_rr=self.public_watchlist_min_rr,
                    public_watchlist_max_per_24h=self.public_watchlist_max_per_24h,
                    public_watchlist_max_per_60m=self.public_watchlist_max_per_60m,
                    public_watchlist_symbol_whitelist=self.public_watchlist_symbol_whitelist,
                    public_watchlist_require_plan=self.public_watchlist_require_plan,
                    public_watchlist_require_entry_zone=self.public_watchlist_require_entry_zone,
                    public_watchlist_require_invalidation=self.public_watchlist_require_invalidation,
                )
                public_watchlist_daily_cap_reached: bool | None = None
                public_watchlist_hourly_cap_reached: bool | None = None
                selected_public_watchlist_scan_ids = _selected_public_watchlist_scan_signal_ids(
                    result.results,
                    eligibility_context,
                    repository=repository,
                    max_per_scan=self.public_watchlist_max_per_scan,
                    cooldown_hours=self.public_watchlist_cooldown_hours,
                    attempted_at=None,
                    daily_cap_reached=public_watchlist_daily_cap_reached,
                    hourly_cap_reached=public_watchlist_hourly_cap_reached,
                )
                public_watchlist_sent_this_scan = 0
                for symbol_result in result.results:
                    audit = _public_watchlist_candidate_audit(
                        symbol_result,
                        eligibility_context,
                        repository=repository,
                    )
                    audit_key = _signal_id(symbol_result)
                    if audit is not None:
                        audit_key = f"{audit_key}|{audit.plan_hash}"
                        public_watchlist_audits[audit_key] = audit
                    alert_type_hint = (
                        _alert_type_for_transition(symbol_result, symbol_result.lifecycle_transition)
                        if symbol_result.lifecycle_transition
                        else None
                    )
                    if alert_type_hint == TelegramAlertType.SIGNAL_CONFIRMED:
                        confirmed_candidates_seen += 1
                        if self.setup_only_public_delivery:
                            confirmed_policy_disabled += 1
                            confirmed_policy_disabled_by_reason[
                                CONFIRMED_PUBLIC_DELIVERY_POLICY_DISABLED_REASON
                            ] = (
                                confirmed_policy_disabled_by_reason.get(
                                    CONFIRMED_PUBLIC_DELIVERY_POLICY_DISABLED_REASON,
                                    0,
                                )
                                + 1
                            )
                        else:
                            confirmed_message = _telegram_signal_message_for_alert(
                                symbol_result,
                                TelegramAlertType.SIGNAL_CONFIRMED,
                                eligibility_context,
                            )
                            confirmed_prefilter = _confirmed_alert_attempt_prefilter(
                                symbol_result,
                                confirmed_message,
                                eligibility_context,
                            )
                            if confirmed_prefilter.passed:
                                confirmed_prefilter_passed += 1
                            else:
                                for reason in confirmed_prefilter.reason_buckets:
                                    confirmed_blocked_before_attempt[reason] = (
                                        confirmed_blocked_before_attempt.get(reason, 0) + 1
                                    )
                    if self.setup_only_public_delivery and alert_type_hint == TelegramAlertType.SIGNAL_CONFIRMED:
                        continue
                    delivery = await self.deliver_for_symbol(
                        symbol_result,
                        repository=repository,
                        scan_run_id=scan_run_id,
                        eligibility_context=eligibility_context,
                        allow_public_watchlist=_public_watchlist_scan_signal_id(symbol_result, eligibility_context) in selected_public_watchlist_scan_ids,
                        public_watchlist_daily_cap_reached=public_watchlist_daily_cap_reached,
                        public_watchlist_hourly_cap_reached=public_watchlist_hourly_cap_reached,
                    )
                    if delivery is None:
                        if audit is not None and audit.eligible:
                            public_watchlist_audits[audit_key] = replace(
                                audit,
                                delivery_status="skipped",
                                skip_reason="no_send_attempt",
                            )
                        ineligible += 1
                        continue
                    record_delivery(delivery)
                    if delivery.alert_type == TelegramAlertType.WATCHLIST.value and audit is not None:
                        skip_reason = delivery.error_message if delivery.status != "sent" else NA
                        if skip_reason == PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON and delivery.detail != NA:
                            skip_reason = f"{skip_reason};{delivery.detail}"
                        public_watchlist_audits[audit_key] = replace(
                            audit,
                            delivery_status=delivery.status,
                            skip_reason=skip_reason,
                        )
                    if delivery.alert_type == TelegramAlertType.WATCHLIST.value and delivery.status == "sent":
                        public_watchlist_sent_this_scan += 1
                for delivery in await self.reconcile_sent_watchlists(
                    repository=repository,
                    lifecycle_repository=lifecycle_repository,
                    sent_watchlists=prior_sent_watchlists,
                    current_results=result.results,
                    scan_run_id=scan_run_id,
                    eligibility_context=eligibility_context,
                    current_run_attempts=frozenset(current_run_attempts),
                    current_run_identity_blocked_symbols=frozenset(current_run_identity_blocked_symbols),
                ):
                    record_delivery(delivery)
                for delivery in await self.maybe_send_research_watch_alerts(
                    result,
                    repository=repository,
                    scan_run_id=scan_run_id,
                ):
                    record_delivery(delivery)

        public_watchlist_audit = _public_watchlist_audit_summary(public_watchlist_audits.values())
        confirmed_alert_audit = ConfirmedAlertAuditSummary(
            public_signal_policy=self.public_signal_policy,
            confirmed_candidates_seen=confirmed_candidates_seen,
            confirmed_prefilter_passed=confirmed_prefilter_passed,
            confirmed_policy_disabled=confirmed_policy_disabled,
            signal_confirmed_attempts_created=signal_confirmed_attempts_created,
            signal_confirmed_sent=signal_confirmed_sent,
            policy_disabled_by_reason=dict(sorted(confirmed_policy_disabled_by_reason.items())),
            blocked_before_attempt_by_reason=dict(sorted(confirmed_blocked_before_attempt.items())),
        )
        _log_public_watchlist_audit(public_watchlist_audit)
        _log_confirmed_alert_audit(confirmed_alert_audit)
        return TelegramLifecycleDeliverySummary(
            attempted=sent + skipped + failed + blocked + blocked_repeat,
            sent=sent,
            skipped=skipped,
            failed=failed,
            blocked=blocked,
            blocked_repeat=blocked_repeat,
            duplicate=duplicate,
            ineligible=ineligible,
            deliveries=tuple(deliveries),
            public_watchlist_audit=public_watchlist_audit,
            confirmed_alert_audit=confirmed_alert_audit,
        )

    async def deliver_for_symbol(
        self,
        symbol_result: ScannerSymbolResult,
        *,
        repository: SQLiteTelegramAlertAttemptRepository,
        scan_run_id: str | None = None,
        eligibility_context: TelegramEligibilityContext | None = None,
        allow_public_watchlist: bool = True,
        public_watchlist_daily_cap_reached: bool | None = None,
        public_watchlist_hourly_cap_reached: bool | None = None,
    ) -> TelegramLifecycleDelivery | None:
        alert_type_hint = (
            _alert_type_for_transition(symbol_result, symbol_result.lifecycle_transition)
            if symbol_result.lifecycle_transition
            else None
        )
        if (
            self.setup_only_public_delivery
            and alert_type_hint is not None
            and alert_type_hint != TelegramAlertType.WATCHLIST
        ):
            return None
        if (
            self.setup_only_public_delivery
            and alert_type_hint == TelegramAlertType.WATCHLIST
            and symbol_result.lifecycle_transition is not None
            and symbol_result.lifecycle_transition.to_state not in WATCH_ALERT_STATES
        ):
            return None
        terminal_bridge = (
            _terminal_identity_bridge(repository, symbol_result, alert_type_hint)
            if alert_type_hint in TERMINAL_UPDATE_ALERT_TYPES
            else _terminal_identity_bridge(
                repository,
                symbol_result,
                alert_type_hint,
                watchlist_only=True,
            )
            if alert_type_hint == TelegramAlertType.SIGNAL_CONFIRMED
            else TerminalIdentityBridge()
        )
        prior_active_alert = terminal_bridge.prior_alert
        prior_public_signal_alert = (
            repository.get_prior_public_signal_alert(signal_ids=(_signal_id(symbol_result),))
            if alert_type_hint == TelegramAlertType.LIMIT_HIT
            else None
        )
        previously_active_sent = (
            prior_active_alert is not None
            if alert_type_hint in TERMINAL_UPDATE_ALERT_TYPES
            else prior_public_signal_alert is not None
            if alert_type_hint == TelegramAlertType.LIMIT_HIT
            else repository.has_prior_active_alert(signal_id=_signal_id(symbol_result))
        )
        context = _minimum_rr_context_for_symbol(
            symbol_result, eligibility_context or TelegramEligibilityContext()
        )
        decision = telegram_alert_decision_for_symbol(
            symbol_result,
            previously_active_sent=previously_active_sent,
            eligibility_context=context,
            terminal_identity_failure_reason=terminal_bridge.blocked_reason,
            prior_public_alert=prior_public_signal_alert if alert_type_hint == TelegramAlertType.LIMIT_HIT else prior_active_alert,
        )
        if _can_try_public_watchlist_reconciliation(decision, alert_type_hint):
            reconciled_decision = _public_watchlist_reconciliation_decision_for_symbol(symbol_result, context)
            if reconciled_decision is not None:
                decision = reconciled_decision
        if not decision.eligible or decision.alert_type is None or decision.message is None:
            if decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED and decision.message is not None:
                prefilter = _confirmed_alert_attempt_prefilter(symbol_result, decision.message, context)
                if not prefilter.passed:
                    return None
            if decision.alert_type == TelegramAlertType.WATCHLIST and decision.message is not None:
                prefilter = _public_watchlist_attempt_prefilter(
                    symbol_result,
                    decision.message,
                    context,
                )
                if not prefilter.passed:
                    return None
            if decision.alert_type is not None and decision.message is not None and _persist_blocked_decision(decision):
                return _persist_blocked_attempt(
                    repository,
                    symbol_result,
                    decision=decision,
                    scan_run_id=scan_run_id,
                    eligibility_context=context,
                )
            return None

        decision = _apply_soft_failed_confirmation_grace_to_decision(
            repository,
            symbol_result,
            decision=decision,
            prior_alert=prior_active_alert,
            scan_run_id=scan_run_id,
            eligibility_context=context,
        )
        if decision is None:
            return None
        if decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED and decision.message is not None:
            prefilter = _confirmed_alert_attempt_prefilter(symbol_result, decision.message, context)
            if not prefilter.passed:
                return None
        if decision.alert_type == TelegramAlertType.WATCHLIST:
            raw_watchlist_signal_id = _signal_id(symbol_result)
            if (
                decision.reason == "eligible_public_watchlist_reconciliation"
                and repository.has_attempt(
                    signal_id=raw_watchlist_signal_id,
                    alert_type=TelegramAlertType.WATCHLIST,
                )
            ):
                return None
            rate_block_reason = _public_watchlist_delivery_rate_block_reason(
                repository,
                decision.message,
                context,
                allow_public_watchlist=allow_public_watchlist,
                daily_cap_reached=public_watchlist_daily_cap_reached,
                hourly_cap_reached=public_watchlist_hourly_cap_reached,
                attempted_at=now_utc_iso(),
            )
            if rate_block_reason is not None:
                return _persist_skipped_public_watchlist_attempt(
                    repository,
                    symbol_result,
                    decision=decision,
                    reason=rate_block_reason,
                    scan_run_id=scan_run_id,
                    eligibility_context=context,
                )
        if (
            _public_watchlist_follow_up_alert_suppressed(decision.alert_type)
            and prior_active_alert is not None
            and (
                self.setup_only_public_delivery
                or prior_active_alert.alert_type == TelegramAlertType.WATCHLIST.value
            )
        ):
            return _persist_suppressed_watchlist_terminal_update(
                repository,
                prior_alert=prior_active_alert,
                symbol_result=symbol_result,
                alert_type=decision.alert_type,
                message=decision.message,
                scan_run_id=scan_run_id,
                eligibility_context=context,
            )
        if self.setup_only_public_delivery and _public_watchlist_follow_up_alert_suppressed(decision.alert_type):
            return None

        message = decision.message
        signal_id = _delivery_signal_id_for_decision(symbol_result, decision)
        if decision.alert_type in TERMINAL_UPDATE_ALERT_TYPES and prior_active_alert is not None:
            signal_id = prior_active_alert.signal_id
            message = replace(
                _message_with_prior_public_identity(message, prior_active_alert),
                was_watchlist=prior_active_alert.alert_type == TelegramAlertType.WATCHLIST.value,
            )
            if repository.has_sent_terminal_outcome(signal_id=signal_id) and not repository.has_attempt(
                signal_id=signal_id,
                alert_type=decision.alert_type,
            ):
                return TelegramLifecycleDelivery(
                    symbol=symbol_result.symbol,
                    signal_id=signal_id,
                    alert_type=decision.alert_type.value,
                    status="duplicate",
                    detail="Prior terminal Telegram lifecycle update already sent.",
                )
        elif decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED and prior_active_alert is not None:
            signal_id = prior_active_alert.signal_id
            message = replace(
                _message_with_prior_public_identity(message, prior_active_alert),
                upgraded_from_watchlist=prior_active_alert.alert_type == TelegramAlertType.WATCHLIST.value,
            )
        elif decision.alert_type == TelegramAlertType.LIMIT_HIT:
            prior_limit_alert = prior_public_signal_alert or repository.get_prior_public_signal_alert(signal_ids=(signal_id,))
            if prior_limit_alert is None:
                reason = "blocked:limit_hit_requires_prior_public_signal"
                blocked_decision = replace(decision, eligible=False, reason=reason, message=message)
                return _persist_blocked_attempt(
                    repository,
                    symbol_result,
                    decision=blocked_decision,
                    scan_run_id=scan_run_id,
                    eligibility_context=context,
                )
            signal_id = prior_limit_alert.signal_id
            message = _message_with_prior_public_plan(message, prior_limit_alert)
        elif decision.alert_type in TP_SL_ALERT_TYPES:
            prior_tp_sl_alert = repository.get_prior_public_alert(signal_ids=(signal_id,))
            if prior_tp_sl_alert is not None:
                signal_id = prior_tp_sl_alert.signal_id
                message = _message_with_prior_public_plan(message, prior_tp_sl_alert)

        if decision.alert_type in TP_SL_ALERT_TYPES:
            blockers = _tp_sl_delivery_blockers(
                repository,
                symbol_result=symbol_result,
                signal_id=signal_id,
                alert_type=decision.alert_type,
                message=message,
            )
            if blockers:
                reason = f"blocked:{'; '.join(blockers)}"
                blocked_decision = replace(decision, eligible=False, reason=reason, message=message)
                _log_lifecycle_alert_audit(
                    symbol_result=symbol_result,
                    message=message,
                    alert_type=decision.alert_type,
                    decision="blocked",
                    reason=reason,
                )
                return _persist_blocked_attempt(
                    repository,
                    symbol_result,
                    decision=blocked_decision,
                    scan_run_id=scan_run_id,
                    eligibility_context=context,
                )

        message_text = format_telegram_signal_message(decision.alert_type, message)
        message_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        transition = decision.lifecycle_transition
        previous_state = transition.from_state.value if transition and transition.from_state else NA
        new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
        attempted_at = now_utc_iso()
        scan_identity = scan_run_id or _transition_scan_run_id(transition)
        reservation: PublicWatchlistReservationResult | None = None
        if decision.alert_type == TelegramAlertType.WATCHLIST:
            plan = _public_watchlist_canonical_plan(symbol_result, message)
            event_key = _public_watchlist_event_key(plan.plan_id, PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE)
            reservation_record = TelegramAlertAttemptRecord(
                signal_id=signal_id,
                symbol=symbol_result.symbol,
                direction=message.direction,
                previous_state=previous_state,
                new_state=new_state,
                alert_type=decision.alert_type.value,
                lifecycle_state=_lifecycle_state_text(symbol_result),
                sent_at=None,
                attempted_at=attempted_at,
                telegram_status=PUBLIC_WATCHLIST_RESERVATION_STATUS,
                message_hash=message_hash,
                scan_run_id=scan_identity,
                attempted_alert_type=decision.alert_type.value,
                setup_quality_score=_quality_score(symbol_result),
                rr_planned=_text(message.planned_rr),
                min_rr=_text(_min_rr_for_alert(decision.alert_type, context)),
                opportunity_score=_opportunity_score_text(symbol_result),
                min_score_for_idea=_text(context.min_score_for_idea),
                technical_score=_technical_score_text(symbol_result),
                price_level=_price_level_for_alert(decision.alert_type, message),
                **_message_level_metadata(message),
                **_public_watchlist_record_plan_fields(symbol_result, decision.alert_type, message),
                blocked_reason=NA,
                invalid_target_fields=NA,
                error_message=NA,
                first_seen_at=attempted_at,
                last_seen_at=attempted_at,
                last_scan_run_id=scan_identity,
                last_error_message=NA,
                dedupe_status=PUBLIC_WATCHLIST_RESERVATION_STATUS,
                dedupe_reason=NA,
            )
            reservation = reserve_public_watchlist_event(
                repository,
                symbol=symbol_result.symbol,
                side=message.direction,
                canonical_plan_id=plan.plan_id,
                event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
                payload={
                    "canonical_plan": plan,
                    "event_key": event_key,
                    "reservation_record": reservation_record,
                    "legacy_since": _public_watchlist_cooldown_cutoff(attempted_at, self.public_watchlist_cooldown_hours)
                    if self.public_watchlist_cooldown_hours > 0
                    else None,
                },
            )
            if not reservation.granted:
                return TelegramLifecycleDelivery(
                    symbol=symbol_result.symbol,
                    signal_id=signal_id,
                    alert_type=decision.alert_type.value,
                    status=reservation.status,
                    detail=reservation.detail,
                    message_hash=message_hash,
                    error_message=reservation.reason,
                )
            if reservation.reservation_id is None or reservation.event_id is None:
                return _persist_public_watchlist_final_send_guard_skip(
                    repository,
                    symbol_result,
                    decision=decision,
                    reservation=reservation,
                    scan_run_id=scan_run_id,
                    eligibility_context=context,
                )
            return await _deliver_committed_public_watchlist_intent(
                repository=repository, sender=self.sender, reservation=reservation,
                symbol_result=symbol_result, signal_id=signal_id, message=message,
                message_text=message_text, message_hash=message_hash, attempted_at=attempted_at,
            )
        elif repository.has_attempt(signal_id=signal_id, alert_type=decision.alert_type):
            repository.compact_existing_attempt(
                signal_id=signal_id,
                alert_type=decision.alert_type,
                scan_run_id=scan_run_id,
            )
            return TelegramLifecycleDelivery(
                symbol=symbol_result.symbol,
                signal_id=signal_id,
                alert_type=decision.alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram alert prevented.",
            )

        public_watchlist_guard: PublicWatchlistSendGuard | None = None
        if decision.alert_type == TelegramAlertType.WATCHLIST:
            if (
                reservation is None
                or not reservation.granted
                or reservation.reservation_id is None
                or reservation.event_id is None
            ):
                return _persist_public_watchlist_final_send_guard_skip(
                    repository,
                    symbol_result,
                    decision=decision,
                    reservation=reservation,
                    scan_run_id=scan_run_id,
                    eligibility_context=context,
                )
            public_watchlist_guard = PublicWatchlistSendGuard(
                event_key=reservation.event_key,
                reservation_id=reservation.reservation_id,
                event_id=reservation.event_id,
                attempt_id="legacy-unreachable-watchlist-path",
            )

        repository.commit_before_network_activity()
        send_result = await self.sender.send_text(
            message_text,
            message_type=_telegram_message_type_for_alert(decision.alert_type, message),
            public_watchlist_guard=public_watchlist_guard,
        )
        if reservation is not None:
            if reservation.reservation_id is None:
                return TelegramLifecycleDelivery(
                    symbol=symbol_result.symbol,
                    signal_id=signal_id,
                    alert_type=decision.alert_type.value,
                    status="duplicate",
                    detail="Public watchlist reservation was not available after acquisition.",
                    message_hash=message_hash,
                    error_message=PUBLIC_WATCHLIST_RESERVATION_IN_FLIGHT_REASON,
                )
            if reservation.event_id is None:
                return TelegramLifecycleDelivery(
                    symbol=symbol_result.symbol,
                    signal_id=signal_id,
                    alert_type=decision.alert_type.value,
                    status="failed",
                    detail="Public alert event ledger reservation was not available after acquisition.",
                    message_hash=message_hash,
                    error_message="public_alert_event_reservation_missing",
                )
            result_reason = NA if send_result.status == "sent" else _first_non_na(send_result.error_message, send_result.detail)
            marked = repository.mark_public_watchlist_reservation_result(
                attempt_id=reservation.reservation_id,
                status=send_result.status,
                sent_at=attempted_at if send_result.status == "sent" else None,
                message_hash=message_hash,
                error_message=result_reason,
                dedupe_status="sent" if send_result.status == "sent" else f"{send_result.status}_retryable",
                dedupe_reason=NA if send_result.status == "sent" else result_reason,
            )
            if not marked:
                return TelegramLifecycleDelivery(
                    symbol=symbol_result.symbol,
                    signal_id=signal_id,
                    alert_type=decision.alert_type.value,
                    status="failed",
                    detail="Public watchlist reservation result could not be persisted.",
                    message_hash=message_hash,
                    error_message="public_watchlist_reservation_mark_failed",
                )
            ledger_marked = _mark_public_alert_event_result(
                repository,
                event_id=reservation.event_id,
                status=send_result.status,
                sent_at=attempted_at if send_result.status == "sent" else None,
                failure_reason=result_reason,
            )
            if not ledger_marked:
                return TelegramLifecycleDelivery(
                    symbol=symbol_result.symbol,
                    signal_id=signal_id,
                    alert_type=decision.alert_type.value,
                    status="failed",
                    detail="Public alert event ledger result could not be persisted.",
                    message_hash=message_hash,
                    error_message="public_alert_event_mark_failed",
                )
        else:
            record = TelegramAlertAttemptRecord(
                signal_id=signal_id,
                symbol=symbol_result.symbol,
                direction=message.direction,
                previous_state=previous_state,
                new_state=new_state,
                alert_type=decision.alert_type.value,
                lifecycle_state=_lifecycle_state_text(symbol_result),
                sent_at=attempted_at if send_result.status == "sent" else None,
                attempted_at=attempted_at,
                telegram_status=send_result.status,
                message_hash=message_hash,
                scan_run_id=scan_identity,
                attempted_alert_type=decision.alert_type.value,
                setup_quality_score=_quality_score(symbol_result),
                rr_planned=_text(message.planned_rr),
                min_rr=_text(_min_rr_for_alert(decision.alert_type, context)),
                opportunity_score=_opportunity_score_text(symbol_result),
                min_score_for_idea=_text(context.min_score_for_idea),
                technical_score=_technical_score_text(symbol_result),
                price_level=_price_level_for_alert(decision.alert_type, message),
                **_message_level_metadata(message),
                **_public_watchlist_record_plan_fields(symbol_result, decision.alert_type, message),
                blocked_reason=NA,
                invalid_target_fields=NA,
                error_message=send_result.error_message,
            )
            inserted = repository.insert_attempt(record)
            if not inserted:
                return TelegramLifecycleDelivery(
                    symbol=symbol_result.symbol,
                    signal_id=signal_id,
                    alert_type=decision.alert_type.value,
                    status="duplicate",
                    detail="Duplicate Telegram alert prevented.",
                    message_hash=message_hash,
                )

        _log_lifecycle_alert_audit(
            symbol_result=symbol_result,
            message=message,
            alert_type=decision.alert_type,
            decision=send_result.status,
            reason=send_result.error_message or send_result.detail,
        )
        logger.info(
            "Telegram signal alert attempt persisted: symbol=%s alert_type=%s status=%s message_hash=%s",
            symbol_result.symbol,
            decision.alert_type.value,
            send_result.status,
            message_hash,
        )
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=decision.alert_type.value,
            status=send_result.status,
            detail=send_result.detail,
            message_hash=message_hash,
            error_message=send_result.error_message,
        )

    async def maybe_send_research_watch_alerts(
        self,
        result: ScannerRunResult,
        *,
        repository: SQLiteTelegramAlertAttemptRepository,
        scan_run_id: str | None = None,
    ) -> tuple[TelegramLifecycleDelivery, ...]:
        max_per_scan = self.research_max_per_scan
        if max_per_scan <= 0:
            return ()
        eligibility = ResearchWatchEligibilityConfig(
            min_quality=self.research_min_quality,
            min_readiness=self.research_min_readiness,
        )
        candidates = tuple(
            candidate
            for candidate in (
                _research_watch_candidate(symbol_result, eligibility)
                for symbol_result in result.results
            )
            if candidate is not None
        )
        if not candidates:
            return ()
        selected = tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    -candidate.quality_score,
                    -candidate.readiness_score,
                    candidate.symbol_result.symbol,
                ),
            )
        )

        deliveries: list[TelegramLifecycleDelivery] = []
        sent_this_scan = 0
        send_attempts = 0
        cooldown_skip_audits = 0
        for candidate in selected:
            if sent_this_scan >= max_per_scan:
                break
            attempted_at = now_utc_iso()
            signal_id = _research_watch_signal_id(candidate.signal_id_prefix, attempted_at)
            message_candidate = replace(candidate, message=replace(candidate.message, signal_id=signal_id))
            message_text = format_research_watch_alert(message_candidate)
            message_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()

            if self.setup_only_public_delivery:
                deliveries.append(
                    _persist_research_watch_attempt(
                        repository,
                        message_candidate,
                        signal_id=signal_id,
                        scan_run_id=scan_run_id,
                        attempted_at=attempted_at,
                        status="skipped",
                        detail="Research Watch public delivery is disabled by the setup-only policy.",
                        message_hash=message_hash,
                        blocked_reason=RESEARCH_WATCH_SETUP_ONLY_POLICY_DISABLED_REASON,
                        error_message=RESEARCH_WATCH_SETUP_ONLY_POLICY_DISABLED_REASON,
                    )
                )
                if len(deliveries) >= max_per_scan:
                    break
                continue

            if not self.research_watch_enabled:
                deliveries.append(
                    _persist_research_watch_attempt(
                        repository,
                        message_candidate,
                        signal_id=signal_id,
                        scan_run_id=scan_run_id,
                        attempted_at=attempted_at,
                        status="skipped",
                        detail="Research Watch alerts are disabled.",
                        message_hash=message_hash,
                        blocked_reason=RESEARCH_WATCH_DISABLED_REASON,
                        error_message=RESEARCH_WATCH_DISABLED_REASON,
                    )
                )
                if len(deliveries) >= max_per_scan:
                    break
                continue

            if not self.research_watch_to_public:
                deliveries.append(
                    _persist_research_watch_attempt(
                        repository,
                        message_candidate,
                        signal_id=signal_id,
                        scan_run_id=scan_run_id,
                        attempted_at=attempted_at,
                        status="blocked",
                        detail="Research Watch public delivery is disabled.",
                        message_hash=message_hash,
                        blocked_reason=RESEARCH_WATCH_PUBLIC_DISABLED_REASON,
                        error_message=RESEARCH_WATCH_PUBLIC_DISABLED_REASON,
                    )
                )
                if len(deliveries) >= max_per_scan:
                    break
                continue

            cooldown_minutes = self.research_alert_cooldown_minutes
            if cooldown_minutes > 0:
                cutoff = _research_watch_cooldown_cutoff(attempted_at, cooldown_minutes)
                if repository.has_recent_research_watch_alert(symbol=candidate.symbol_result.symbol, since=cutoff):
                    if cooldown_skip_audits < max_per_scan:
                        cooldown_signal_id = _research_watch_cooldown_signal_id(candidate.symbol_result.symbol)
                        deliveries.append(
                            _persist_research_watch_attempt(
                                repository,
                                message_candidate,
                                signal_id=cooldown_signal_id,
                                scan_run_id=scan_run_id,
                                attempted_at=attempted_at,
                                status="skipped",
                                detail="Research Watch cooldown is active for this symbol.",
                                message_hash=message_hash,
                                blocked_reason=RESEARCH_WATCH_COOLDOWN_REASON,
                                error_message=RESEARCH_WATCH_COOLDOWN_REASON,
                            )
                        )
                        cooldown_skip_audits += 1
                    continue

            if send_attempts >= max_per_scan:
                break
            send_attempts += 1
            repository.commit_before_network_activity()
            send_result = await self.sender.send_text(
                message_text,
                message_type=TelegramMessageType.RESEARCH_WATCH,
            )
            reason = NA if send_result.status == "sent" else _first_non_na(send_result.error_message, send_result.detail)
            deliveries.append(
                _persist_research_watch_attempt(
                    repository,
                    message_candidate,
                    signal_id=signal_id,
                    scan_run_id=scan_run_id,
                    attempted_at=attempted_at,
                    status=send_result.status,
                    detail=send_result.detail,
                    message_hash=message_hash,
                    blocked_reason=reason,
                    error_message=send_result.error_message,
                )
            )
            if send_result.status == "sent":
                sent_this_scan += 1
        return tuple(deliveries)

    async def reconcile_sent_watchlists(
        self,
        *,
        repository: SQLiteTelegramAlertAttemptRepository,
        lifecycle_repository: SQLiteSetupLifecycleRepository,
        sent_watchlists: Sequence[TelegramAlertAttemptRecord],
        current_results: Sequence[ScannerSymbolResult],
        scan_run_id: str | None,
        eligibility_context: TelegramEligibilityContext,
        current_run_attempts: frozenset[tuple[str, str]],
        current_run_identity_blocked_symbols: frozenset[str],
    ) -> tuple[TelegramLifecycleDelivery, ...]:
        deliveries: list[TelegramLifecycleDelivery] = []
        snapshot_watchlists_by_symbol = _sent_watchlist_snapshot_by_symbol(sent_watchlists)
        for prior_alert in sent_watchlists:
            if prior_alert.alert_type != TelegramAlertType.WATCHLIST.value or prior_alert.telegram_status != "sent":
                continue
            if _symbol(prior_alert.symbol) in current_run_identity_blocked_symbols:
                continue
            if repository.has_sent_terminal_outcome(signal_id=prior_alert.signal_id):
                continue
            expiry = _prior_watchlist_expiry_decision(repository, prior_alert)
            if expiry.expired:
                deliveries.append(
                    _persist_watchlist_expiry_audit(
                        repository,
                        prior_alert,
                        reason=WATCHLIST_EXPIRY_REASON,
                        scan_run_id=scan_run_id,
                    )
                )
                continue
            if expiry.reason in {WATCHLIST_EXPIRY_TIMESTAMP_NA_REASON, WATCHLIST_EXPIRY_TIMESTAMP_UNVERIFIED_REASON}:
                _persist_watchlist_expiry_audit(
                    repository,
                    prior_alert,
                    reason=expiry.reason,
                    scan_run_id=scan_run_id,
                )
            if _has_current_run_soft_failed_confirmation_observation(
                repository,
                signal_id=prior_alert.signal_id,
                scan_run_id=scan_run_id,
            ):
                continue
            if (prior_alert.signal_id, TelegramAlertType.WATCHLIST.value) in current_run_attempts:
                continue

            current_result_for_prior = _current_result_for_prior_watchlist(prior_alert, current_results)
            if (
                self.watchlist_outcome_tracking_enabled
                and current_result_for_prior is not None
                and current_result_for_prior.lifecycle_outcome_progress is None
            ):
                outcome_delivery = await self._send_watchlist_outcome_update(
                    repository,
                    prior_alert=prior_alert,
                    current_result=current_result_for_prior,
                    scan_run_id=scan_run_id,
                    eligibility_context=eligibility_context,
                )
                if outcome_delivery is not None:
                    deliveries.append(outcome_delivery)
                    if outcome_delivery.alert_type in {
                        alert_type.value for alert_type in WATCHLIST_OUTCOME_TERMINAL_ALERT_TYPES
                    }:
                        continue

            match = _match_sent_watchlist_lifecycle(
                prior_alert,
                lifecycle_repository=lifecycle_repository,
                current_results=current_results,
                snapshot_watchlists_by_symbol=snapshot_watchlists_by_symbol,
            )
            if match.blocked_reason is not None:
                deliveries.append(
                    _persist_sent_watchlist_reconciliation_block(
                        repository,
                        prior_alert,
                        reason=match.blocked_reason,
                        scan_run_id=scan_run_id,
                    )
                )
                continue
            if match.record is None:
                continue
            if repository.has_canonical_lifecycle_outcome_progress(lifecycle_id=match.record.lifecycle_id):
                continue

            current_result = _current_result_for_lifecycle_record(match.record, prior_alert, current_results)
            if current_result is None:
                current_result = current_result_for_prior
            if (
                self.setup_only_public_delivery
                and _terminal_alert_type_for_lifecycle_state(
                    _lifecycle_record_state_key(match.record)
                ) is None
            ):
                continue
            outcome = _sent_watchlist_reconciliation_outcome(
                prior_alert,
                match.record,
                current_result=current_result,
                eligibility_context=eligibility_context,
            )
            if outcome is None:
                continue

            outcome = _apply_soft_failed_confirmation_grace_to_reconciliation(
                repository,
                prior_alert=prior_alert,
                outcome=outcome,
                scan_run_id=scan_run_id,
                eligibility_context=eligibility_context,
            )
            if outcome is None:
                continue
            if (
                _public_watchlist_follow_up_alert_suppressed(outcome.alert_type)
                and prior_alert.alert_type == TelegramAlertType.WATCHLIST.value
            ):
                deliveries.append(
                    _persist_suppressed_watchlist_terminal_update(
                        repository,
                        prior_alert=prior_alert,
                        symbol_result=outcome.symbol_result,
                        alert_type=outcome.alert_type,
                        message=outcome.message,
                        scan_run_id=scan_run_id,
                        eligibility_context=eligibility_context,
                    )
                )
                continue

            signal_id = prior_alert.signal_id
            alert_type_text = outcome.alert_type.value
            if (signal_id, alert_type_text) in current_run_attempts and repository.has_attempt(
                signal_id=signal_id,
                alert_type=outcome.alert_type,
            ):
                continue
            if outcome.alert_type in TERMINAL_UPDATE_ALERT_TYPES and repository.has_sent_terminal_outcome(
                signal_id=signal_id
            ) and not repository.has_attempt(signal_id=signal_id, alert_type=outcome.alert_type):
                deliveries.append(
                    TelegramLifecycleDelivery(
                        symbol=prior_alert.symbol,
                        signal_id=signal_id,
                        alert_type=alert_type_text,
                        status="duplicate",
                        detail="Prior terminal Telegram lifecycle update already sent.",
                    )
                )
                continue
            if repository.has_attempt(signal_id=signal_id, alert_type=outcome.alert_type):
                repository.compact_existing_attempt(
                    signal_id=signal_id,
                    alert_type=outcome.alert_type,
                    scan_run_id=scan_run_id,
                )
                deliveries.append(
                    TelegramLifecycleDelivery(
                        symbol=prior_alert.symbol,
                        signal_id=signal_id,
                        alert_type=alert_type_text,
                        status="duplicate",
                        detail="Duplicate Telegram alert prevented.",
                    )
                )
                continue

            deliveries.append(
                await self._send_reconciliation_update(
                    repository,
                    prior_alert=prior_alert,
                    outcome=outcome,
                    scan_run_id=scan_run_id,
                    eligibility_context=eligibility_context,
                )
            )
        return tuple(deliveries)

    async def _send_watchlist_outcome_update(
        self,
        repository: SQLiteTelegramAlertAttemptRepository,
        *,
        prior_alert: TelegramAlertAttemptRecord,
        current_result: ScannerSymbolResult,
        scan_run_id: str | None,
        eligibility_context: TelegramEligibilityContext,
    ) -> TelegramLifecycleDelivery | None:
        outcome = _watchlist_outcome_for_current_result(
            repository,
            prior_alert=prior_alert,
            current_result=current_result,
            eligibility_context=eligibility_context,
            scan_run_id=scan_run_id,
        )
        if outcome is None:
            return None
        if isinstance(outcome, TelegramLifecycleDelivery):
            return outcome

        alert_type, message = outcome
        if _public_watchlist_follow_up_alert_suppressed(alert_type):
            return _persist_suppressed_watchlist_terminal_update(
                repository,
                prior_alert=prior_alert,
                symbol_result=current_result,
                alert_type=alert_type,
                message=message,
                scan_run_id=scan_run_id,
                eligibility_context=eligibility_context,
            )
        if repository.has_attempt(signal_id=prior_alert.signal_id, alert_type=alert_type):
            repository.compact_existing_attempt(
                signal_id=prior_alert.signal_id,
                alert_type=alert_type,
                scan_run_id=scan_run_id,
            )
            return TelegramLifecycleDelivery(
                symbol=prior_alert.symbol,
                signal_id=prior_alert.signal_id,
                alert_type=alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram watchlist outcome update prevented.",
            )

        message_text = format_telegram_signal_message(alert_type, message)
        message_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        repository.commit_before_network_activity()
        send_result = await self.sender.send_text(
            message_text,
            message_type=_telegram_message_type_for_alert(alert_type, message),
        )
        transition = current_result.lifecycle_transition
        previous_state = transition.from_state.value if transition and transition.from_state else prior_alert.new_state
        new_state = transition.to_state.value if transition else _lifecycle_state_text(current_result)
        attempted_at = now_utc_iso()
        record = TelegramAlertAttemptRecord(
            signal_id=prior_alert.signal_id,
            symbol=_symbol(message.symbol),
            direction=_text(message.direction),
            previous_state=previous_state,
            new_state=new_state,
            alert_type=alert_type.value,
            lifecycle_state=_lifecycle_state_text(current_result),
            sent_at=attempted_at if send_result.status == "sent" else None,
            attempted_at=attempted_at,
            telegram_status=send_result.status,
            message_hash=message_hash,
            scan_run_id=scan_run_id or _transition_scan_run_id(transition),
            attempted_alert_type=alert_type.value,
            setup_quality_score=_quality_score(current_result),
            rr_planned=_text(message.planned_rr),
            min_rr=_text(eligibility_context.min_rr),
            opportunity_score=_opportunity_score_text(current_result),
            min_score_for_idea=_text(eligibility_context.min_score_for_idea),
            technical_score=_technical_score_text(current_result),
            price_level=_price_level_for_alert(alert_type, message),
            **_message_level_metadata(message),
            blocked_reason=NA,
            invalid_target_fields=NA,
            error_message=send_result.error_message,
        )
        inserted = repository.insert_attempt(record)
        if not inserted:
            return TelegramLifecycleDelivery(
                symbol=prior_alert.symbol,
                signal_id=prior_alert.signal_id,
                alert_type=alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram watchlist outcome update prevented.",
                message_hash=message_hash,
            )
        logger.info(
            "Telegram watchlist outcome persisted: symbol=%s alert_type=%s status=%s message_hash=%s",
            prior_alert.symbol,
            alert_type.value,
            send_result.status,
            message_hash,
        )
        _log_lifecycle_alert_audit(
            symbol_result=current_result,
            message=message,
            alert_type=alert_type,
            decision=send_result.status,
            reason=send_result.error_message or send_result.detail,
        )
        return TelegramLifecycleDelivery(
            symbol=prior_alert.symbol,
            signal_id=prior_alert.signal_id,
            alert_type=alert_type.value,
            status=send_result.status,
            detail=send_result.detail,
            message_hash=message_hash,
            error_message=send_result.error_message,
        )

    async def _send_reconciliation_update(
        self,
        repository: SQLiteTelegramAlertAttemptRepository,
        *,
        prior_alert: TelegramAlertAttemptRecord,
        outcome: SentWatchlistReconciliationOutcome,
        scan_run_id: str | None,
        eligibility_context: TelegramEligibilityContext,
    ) -> TelegramLifecycleDelivery:
        message_text = format_telegram_signal_message(outcome.alert_type, outcome.message)
        message_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        repository.commit_before_network_activity()
        send_result = await self.sender.send_text(
            message_text,
            message_type=_telegram_message_type_for_alert(outcome.alert_type, outcome.message),
        )
        transition = outcome.symbol_result.lifecycle_transition
        previous_state = transition.from_state.value if transition and transition.from_state else NA
        new_state = transition.to_state.value if transition else _lifecycle_state_text(outcome.symbol_result)
        attempted_at = now_utc_iso()
        record = TelegramAlertAttemptRecord(
            signal_id=prior_alert.signal_id,
            symbol=_symbol(outcome.message.symbol),
            direction=_text(outcome.message.direction),
            previous_state=previous_state,
            new_state=new_state,
            alert_type=outcome.alert_type.value,
            lifecycle_state=_lifecycle_state_text(outcome.symbol_result),
            sent_at=attempted_at if send_result.status == "sent" else None,
            attempted_at=attempted_at,
            telegram_status=send_result.status,
            message_hash=message_hash,
            scan_run_id=scan_run_id or _transition_scan_run_id(transition),
            attempted_alert_type=outcome.alert_type.value,
            setup_quality_score=_quality_score(outcome.symbol_result),
            rr_planned=_text(outcome.message.planned_rr),
            min_rr=_text(eligibility_context.min_rr),
            opportunity_score=_opportunity_score_text(outcome.symbol_result),
            min_score_for_idea=_text(eligibility_context.min_score_for_idea),
            technical_score=_technical_score_text(outcome.symbol_result),
            price_level=_price_level_for_alert(outcome.alert_type, outcome.message),
            **_message_level_metadata(outcome.message),
            blocked_reason=NA,
            invalid_target_fields=NA,
            error_message=send_result.error_message,
        )
        inserted = repository.insert_attempt(record)
        if not inserted:
            return TelegramLifecycleDelivery(
                symbol=prior_alert.symbol,
                signal_id=prior_alert.signal_id,
                alert_type=outcome.alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram alert prevented.",
                message_hash=message_hash,
            )
        logger.info(
            "Telegram sent-watchlist reconciliation persisted: symbol=%s alert_type=%s status=%s message_hash=%s",
            prior_alert.symbol,
            outcome.alert_type.value,
            send_result.status,
            message_hash,
        )
        return TelegramLifecycleDelivery(
            symbol=prior_alert.symbol,
            signal_id=prior_alert.signal_id,
            alert_type=outcome.alert_type.value,
            status=send_result.status,
            detail=send_result.detail,
            message_hash=message_hash,
            error_message=send_result.error_message,
        )


def telegram_alert_decision_for_symbol(
    symbol_result: ScannerSymbolResult,
    *,
    previously_active_sent: bool = False,
    eligibility_context: TelegramEligibilityContext | None = None,
    terminal_identity_failure_reason: str | None = None,
    prior_public_alert: TelegramAlertAttemptRecord | None = None,
) -> TelegramAlertDecision:
    transition = symbol_result.lifecycle_transition
    if transition is None:
        return TelegramAlertDecision(False, "missing_lifecycle_transition")
    if not transition.transitioned:
        return TelegramAlertDecision(False, "unchanged_lifecycle_state", lifecycle_transition=transition)
    alert_type = _alert_type_for_transition(symbol_result, transition)
    if alert_type is None:
        return TelegramAlertDecision(False, "lifecycle_state_not_eligible", lifecycle_transition=transition)

    context = _minimum_rr_context_for_symbol(
        symbol_result, eligibility_context or TelegramEligibilityContext()
    )
    message = _telegram_signal_message_for_alert(symbol_result, alert_type, context)
    if alert_type in TERMINAL_UPDATE_ALERT_TYPES and prior_public_alert is not None:
        message = replace(
            _message_with_prior_public_identity(message, prior_public_alert),
            was_watchlist=prior_public_alert.alert_type == TelegramAlertType.WATCHLIST.value,
        )
    if alert_type == TelegramAlertType.LIMIT_HIT and prior_public_alert is not None:
        message = _message_with_prior_public_plan(message, prior_public_alert)

    public_gate = _public_signal_gate_result(
        symbol_result,
        alert_type,
        message,
        prior_public_alert=prior_public_alert,
    )
    if not public_gate.allowed:
        return TelegramAlertDecision(
            False,
            "blocked:" + "; ".join(public_gate.blocking_reasons),
            alert_type=alert_type,
            message=message,
            lifecycle_transition=transition,
        )

    if _requires_prior_active_alert(alert_type) and not previously_active_sent:
        if alert_type in TERMINAL_UPDATE_ALERT_TYPES:
            reason = terminal_identity_failure_reason or "terminal_update_no_prior_public_alert"
        elif alert_type == TelegramAlertType.LIMIT_HIT:
            reason = "blocked:limit_hit_requires_prior_public_signal"
        else:
            reason = "missing_prior_active_telegram_alert"
        return TelegramAlertDecision(
            False,
            reason,
            alert_type=alert_type if alert_type in TERMINAL_UPDATE_ALERT_TYPES or alert_type == TelegramAlertType.LIMIT_HIT else None,
            message=message if alert_type in TERMINAL_UPDATE_ALERT_TYPES or alert_type == TelegramAlertType.LIMIT_HIT else None,
            lifecycle_transition=transition,
        )

    blockers = _defensive_delivery_blockers(symbol_result, alert_type, message, context)
    if blockers:
        failed_confirmation = _failed_confirmation_terminal_decision(
            symbol_result,
            alert_type=alert_type,
            message=message,
            blockers=blockers,
            lifecycle_transition=transition,
            prior_public_alert=prior_public_alert,
            terminal_identity_failure_reason=terminal_identity_failure_reason,
        )
        if failed_confirmation is not None:
            return failed_confirmation
        return TelegramAlertDecision(
            False,
            "blocked:" + "; ".join(blockers),
            alert_type=alert_type,
            message=message,
            lifecycle_transition=transition,
        )
    missing = _missing_required_fields(alert_type, message)
    if missing:
        return TelegramAlertDecision(
            False,
            f"missing_required_fields:{','.join(missing)}",
            alert_type=alert_type,
            message=message,
            lifecycle_transition=transition,
        )
    return TelegramAlertDecision(True, "eligible", alert_type=alert_type, message=message, lifecycle_transition=transition)


def _can_try_public_watchlist_reconciliation(
    decision: TelegramAlertDecision,
    alert_type_hint: TelegramAlertType | None,
) -> bool:
    if decision.eligible and decision.alert_type is not None:
        return False
    if alert_type_hint in TERMINAL_UPDATE_ALERT_TYPES or alert_type_hint in {
        TelegramAlertType.SIGNAL_CONFIRMED,
        TelegramAlertType.LIMIT_HIT,
        TelegramAlertType.TP1_HIT,
        TelegramAlertType.TP2_HIT,
        TelegramAlertType.TP3_HIT,
        TelegramAlertType.SL_HIT,
    }:
        return False
    return decision.reason in {
        "missing_lifecycle_transition",
        "unchanged_lifecycle_state",
        "lifecycle_state_not_eligible",
    } or decision.alert_type is None


def _public_watchlist_follow_up_alert_suppressed(alert_type: TelegramAlertType | None) -> bool:
    return alert_type in TERMINAL_UPDATE_ALERT_TYPES or alert_type in TP_SL_ALERT_TYPES or alert_type in {
        TelegramAlertType.SIGNAL_CONFIRMED,
        TelegramAlertType.LIMIT_HIT,
    }

def _public_watchlist_reconciliation_decision_for_symbol(
    symbol_result: ScannerSymbolResult,
    context: TelegramEligibilityContext,
) -> TelegramAlertDecision | None:
    candidate = _public_watchlist_candidate_from_symbol(symbol_result)
    if not _explicit_public_watchlist_source(symbol_result, candidate):
        return None
    message = _telegram_signal_message_for_alert(symbol_result, TelegramAlertType.WATCHLIST, context)
    prefilter = _public_watchlist_attempt_prefilter(symbol_result, message, context)
    if not prefilter.passed:
        return TelegramAlertDecision(
            False,
            "blocked:" + "; ".join(prefilter.blocking_reasons),
            alert_type=TelegramAlertType.WATCHLIST,
            message=message,
        )
    gate = _public_watchlist_gate_result(symbol_result, message, context)
    if not gate.allowed:
        return TelegramAlertDecision(
            False,
            "blocked:" + "; ".join(gate.blocking_reasons),
            alert_type=TelegramAlertType.WATCHLIST,
            message=message,
        )
    return TelegramAlertDecision(
        True,
        "eligible_public_watchlist_reconciliation",
        alert_type=TelegramAlertType.WATCHLIST,
        message=message,
    )

def telegram_signal_message_from_symbol(symbol_result: ScannerSymbolResult) -> TelegramSignalMessage:
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    lifecycle = symbol_result.lifecycle_state
    direction = _first_non_na(
        getattr(lifecycle, "direction", NA),
        getattr(trade_idea, "direction", NA) if trade_idea is not None else NA,
        _field(setup, "bias"),
        diagnostics.get("bias"),
        diagnostics.get("direction"),
    )
    mode = _first_non_na(
        getattr(lifecycle, "mode", NA),
        symbol_result.valid_strategy_modes[0] if symbol_result.valid_strategy_modes else NA,
        symbol_result.rejected_strategy_modes[0] if symbol_result.rejected_strategy_modes else NA,
        _field(setup, "mode"),
        diagnostics.get("mode"),
        getattr(trade_idea, "setup_type", NA) if trade_idea is not None else NA,
    )
    quality = _first_non_na(
        getattr(getattr(symbol_result.setup_quality, "quality_grade", None), "value", NA),
        getattr(trade_idea, "grade", NA) if trade_idea is not None else NA,
        _field(getattr(setup, "trust_meter", None), "grade"),
        diagnostics.get("trust_grade"),
        diagnostics.get("grade"),
    )
    entry_low = _first_non_na(
        getattr(lifecycle, "entry_low", NA),
        _field(setup, "entry_low"),
        diagnostics.get("entry_low"),
        _mapping_value(diagnostics.get("entry_zone"), "low"),
        _mapping_value(diagnostics.get("watch_zone"), "low"),
        _level_field(getattr(trade_idea, "entry_zone", None), "low"),
        _level_field(getattr(trade_idea, "entry_zone", None), "price"),
        diagnostics.get("entry"),
    )
    entry_high = _first_non_na(
        getattr(lifecycle, "entry_high", NA),
        _field(setup, "entry_high"),
        diagnostics.get("entry_high"),
        _mapping_value(diagnostics.get("entry_zone"), "high"),
        _mapping_value(diagnostics.get("watch_zone"), "high"),
        _level_field(getattr(trade_idea, "entry_zone", None), "high"),
        _level_field(getattr(trade_idea, "entry_zone", None), "price"),
        diagnostics.get("entry"),
    )
    stop_loss = _first_non_na(
        getattr(lifecycle, "stop_loss", NA),
        _field(setup, "stop"),
        diagnostics.get("stop"),
        _level_field(getattr(trade_idea, "stop_loss", None), "price"),
        diagnostics.get("stop_loss"),
    )
    lifecycle_invalidation = _first_non_na(
        getattr(lifecycle, "invalidation_reason", NA),
        getattr(lifecycle, "invalidation_logic", NA),
    )
    invalidation_reason = _first_non_na(
        _clean_public_sentence(lifecycle_invalidation),
        _public_invalidation_sentence(
            direction=direction,
            stop_loss=stop_loss,
            entry_low=entry_low,
            entry_high=entry_high,
            raw_invalidation=_first_non_na(
                getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA,
                _field(setup, "invalidation"),
                diagnostics.get("invalidation"),
                _near_miss_invalidation_hint(symbol_result, diagnostics),
                lifecycle_invalidation,
            ),
        ),
    )
    message = TelegramSignalMessage(
        symbol=symbol_result.symbol,
        direction=direction,
        signal_id=_signal_id(symbol_result),
        mode=mode,
        quality=quality,
        watch_zone=_first_non_na(_entry_zone_text(entry_low, entry_high), _watch_zone_text(symbol_result, diagnostics)),
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        tp1=_first_non_na(getattr(lifecycle, "tp1", NA), _field(setup, "tp1"), diagnostics.get("tp1"), _take_profit(trade_idea, 1)),
        tp2=_first_non_na(getattr(lifecycle, "tp2", NA), _field(setup, "tp2"), diagnostics.get("tp2"), _take_profit(trade_idea, 2)),
        tp3=_first_non_na(getattr(lifecycle, "tp3", NA), _field(setup, "tp3"), diagnostics.get("tp3"), _take_profit(trade_idea, 3)),
        planned_rr=_first_non_na(
            getattr(lifecycle, "rr", NA),
            _field(setup, "rr_to_tp2"),
            diagnostics.get("rr_to_tp2"),
            getattr(trade_idea, "best_rr", NA) if trade_idea is not None else NA,
        ),
        current_context=_watchlist_context_sentence(symbol_result, diagnostics),
        needs_next=_watchlist_needs_next(symbol_result, diagnostics),
        structure_reason=_first_non_na(
            getattr(trade_idea, "reason_for_trade", NA) if trade_idea is not None else NA,
            diagnostics.get("structure_reason"),
            diagnostics.get("pullback_failure_reason"),
            diagnostics.get("confirmation_bos_choch_reason"),
            symbol_result.rejection_reason,
        ),
        confirmation_needed=_first_non_na(
            diagnostics.get("confirmation_needed"),
            diagnostics.get("next_trigger_needed"),
            _confirmation_needed(diagnostics),
        ),
        invalidation_reason=invalidation_reason,
        watchlist_invalidation_reason=_watchlist_invalidation_sentence(
            symbol_result,
            diagnostics,
            direction=direction,
            stop_loss=stop_loss,
        ),
        confluence=_human_confluence_sentence(symbol_result, diagnostics),
        htf_bias=_first_non_na(diagnostics.get("htf_2d_trend"), diagnostics.get("mtf_12h_trend")),
        ob_fvg_status=_first_non_na(
            diagnostics.get("selected_zone_type"),
            diagnostics.get("ob_fvg_status"),
            diagnostics.get("ob_fvg_diagnostics"),
        ),
        volume_status=_first_non_na(
            diagnostics.get("volume_profile_source"),
            symbol_result.volume_profile_source,
        ),
        derivatives_status=_derivatives_status(symbol_result, diagnostics),
        price_level=_first_non_na(
            diagnostics.get("price_level"),
            diagnostics.get("exit_price"),
            symbol_result.current_price,
            symbol_result.latest_close,
        ),
        actionability_state=_first_non_na(
            getattr(symbol_result, "actionability_state", NA),
            getattr(lifecycle, "actionability_state", NA),
            diagnostics.get("actionability_state"),
        ),
        target_failure_severity=_first_non_na(
            getattr(symbol_result, "target_failure_severity", NA),
            getattr(lifecycle, "target_failure_severity", NA),
            diagnostics.get("target_failure_severity"),
        ),
        target_warning_reason=_first_non_na(
            getattr(symbol_result, "target_warning_reason", NA),
            getattr(lifecycle, "target_warning_reason", NA),
            diagnostics.get("target_warning_reason"),
            diagnostics.get("target_integrity_warning"),
            diagnostics.get("target_integrity_reason"),
        ),
    )
    return _message_with_signal_context(symbol_result, diagnostics, message)


def _public_watchlist_candidate_from_symbol(symbol_result: ScannerSymbolResult) -> PublicWatchlistCandidate:
    diagnostics = _representative_diagnostics(symbol_result)
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    lifecycle = symbol_result.lifecycle_state
    side = _status_key(
        _first_non_na(
            getattr(lifecycle, "direction", NA) if lifecycle is not None else NA,
            _field(intelligence, "side"),
            _field(intelligence, "bias"),
            _field(intelligence, "direction"),
            diagnostics.get("side"),
            diagnostics.get("bias"),
            diagnostics.get("direction"),
            diagnostics.get("trade_side"),
            _field(setup, "side"),
            _field(setup, "bias"),
            _field(setup, "direction"),
            getattr(trade_idea, "direction", NA) if trade_idea is not None else NA,
        )
    )
    mode = _first_non_na(
        getattr(lifecycle, "mode", NA) if lifecycle is not None else NA,
        diagnostics.get("mode"),
        _field(setup, "mode"),
        symbol_result.valid_strategy_modes[0] if symbol_result.valid_strategy_modes else NA,
        symbol_result.rejected_strategy_modes[0] if symbol_result.rejected_strategy_modes else NA,
        getattr(trade_idea, "setup_type", NA) if trade_idea is not None else NA,
    )
    strategy = _first_non_na(
        symbol_result.strategy_name,
        diagnostics.get("strategy"),
        diagnostics.get("strategy_name"),
        _field(setup, "strategy"),
        getattr(trade_idea, "setup_type", NA) if trade_idea is not None else NA,
    )
    watchlist_grade = _first_normalized_grade(
        _field(intelligence, "watchlist_grade"),
        _field(intelligence, "public_watchlist_grade"),
        _field(intelligence, "readiness_grade"),
        _field(intelligence, "grade"),
        diagnostics.get("watchlist_grade"),
        diagnostics.get("public_watchlist_grade"),
        diagnostics.get("candidate_grade"),
        diagnostics.get("grade"),
        diagnostics.get("readiness_grade"),
        _field(setup, "watchlist_grade"),
    )
    quality_grade_current = _first_non_reject_normalized_grade(
        _field(intelligence, "quality_grade_current"),
        diagnostics.get("quality_grade_current"),
        diagnostics.get("current_quality_grade"),
        diagnostics.get("quality_grade"),
        diagnostics.get("opportunity_grade"),
        _field(setup, "quality_grade_current"),
        _field(setup, "quality_grade"),
        _field(getattr(setup, "trust_meter", None), "grade"),
        getattr(trade_idea, "grade", NA) if trade_idea is not None else NA,
        getattr(getattr(symbol_result.setup_quality, "quality_grade", None), "value", NA),
    )
    readiness_score = _first_non_na(
        _field(intelligence, "readiness_score"),
        _field(intelligence, "quality_score"),
        diagnostics.get("readiness_score"),
        getattr(symbol_result.setup_quality, "quality_score", NA),
        getattr(lifecycle, "readiness_score", NA) if lifecycle is not None else NA,
    )
    grade = _first_non_na(watchlist_grade, quality_grade_current, grade_from_score(readiness_score))
    entry_low = _first_non_na(
        getattr(lifecycle, "entry_low", NA) if lifecycle is not None else NA,
        _field(intelligence, "entry_zone_low"),
        _field(intelligence, "entry_low"),
        _field(intelligence, "limit_zone_low"),
        _field(intelligence, "pullback_zone_low"),
        _field(intelligence, "ob_low"),
        _field(intelligence, "fvg_low"),
        _field(intelligence, "zone_low"),
        diagnostics.get("entry_zone_low"),
        diagnostics.get("limit_zone_low"),
        diagnostics.get("pullback_zone_low"),
        diagnostics.get("ob_low"),
        diagnostics.get("fvg_low"),
        diagnostics.get("zone_low"),
        diagnostics.get("entry_low"),
        _mapping_value(diagnostics.get("entry_zone"), "low"),
        _mapping_value(diagnostics.get("limit_zone"), "low"),
        _mapping_value(diagnostics.get("pullback_zone"), "low"),
        _mapping_value(diagnostics.get("ob_zone"), "low"),
        _mapping_value(diagnostics.get("fvg_zone"), "low"),
        _mapping_value(diagnostics.get("fib_zone"), "low"),
        _mapping_value(diagnostics.get("zone"), "low"),
        _mapping_value(diagnostics.get("watch_zone"), "low"),
        _field(setup, "entry_zone_low"),
        _field(setup, "limit_zone_low"),
        _field(setup, "pullback_zone_low"),
        _field(setup, "ob_low"),
        _field(setup, "fvg_low"),
        _field(setup, "zone_low"),
        _field(setup, "entry_low"),
        _mapping_value(_field(setup, "entry_zone"), "low"),
        _mapping_value(_field(setup, "limit_zone"), "low"),
        _mapping_value(_field(setup, "pullback_zone"), "low"),
        _mapping_value(_field(setup, "ob_zone"), "low"),
        _mapping_value(_field(setup, "fvg_zone"), "low"),
        _mapping_value(_field(setup, "fib_zone"), "low"),
        _mapping_value(_field(setup, "zone"), "low"),
        _level_field(getattr(trade_idea, "entry_zone", None), "low"),
        _level_field(getattr(trade_idea, "entry_zone", None), "price"),
        diagnostics.get("entry"),
    )
    entry_high = _first_non_na(
        getattr(lifecycle, "entry_high", NA) if lifecycle is not None else NA,
        _field(intelligence, "entry_zone_high"),
        _field(intelligence, "entry_high"),
        _field(intelligence, "limit_zone_high"),
        _field(intelligence, "pullback_zone_high"),
        _field(intelligence, "ob_high"),
        _field(intelligence, "fvg_high"),
        _field(intelligence, "zone_high"),
        diagnostics.get("entry_zone_high"),
        diagnostics.get("limit_zone_high"),
        diagnostics.get("pullback_zone_high"),
        diagnostics.get("ob_high"),
        diagnostics.get("fvg_high"),
        diagnostics.get("zone_high"),
        diagnostics.get("entry_high"),
        _mapping_value(diagnostics.get("entry_zone"), "high"),
        _mapping_value(diagnostics.get("limit_zone"), "high"),
        _mapping_value(diagnostics.get("pullback_zone"), "high"),
        _mapping_value(diagnostics.get("ob_zone"), "high"),
        _mapping_value(diagnostics.get("fvg_zone"), "high"),
        _mapping_value(diagnostics.get("fib_zone"), "high"),
        _mapping_value(diagnostics.get("zone"), "high"),
        _mapping_value(diagnostics.get("watch_zone"), "high"),
        _field(setup, "entry_zone_high"),
        _field(setup, "limit_zone_high"),
        _field(setup, "pullback_zone_high"),
        _field(setup, "ob_high"),
        _field(setup, "fvg_high"),
        _field(setup, "zone_high"),
        _field(setup, "entry_high"),
        _mapping_value(_field(setup, "entry_zone"), "high"),
        _mapping_value(_field(setup, "limit_zone"), "high"),
        _mapping_value(_field(setup, "pullback_zone"), "high"),
        _mapping_value(_field(setup, "ob_zone"), "high"),
        _mapping_value(_field(setup, "fvg_zone"), "high"),
        _mapping_value(_field(setup, "fib_zone"), "high"),
        _mapping_value(_field(setup, "zone"), "high"),
        _level_field(getattr(trade_idea, "entry_zone", None), "high"),
        _level_field(getattr(trade_idea, "entry_zone", None), "price"),
        diagnostics.get("entry"),
    )
    parsed_zone = _first_decimal_pair_text(
        _field(intelligence, "entry_zone"),
        _field(intelligence, "limit_zone"),
        _field(intelligence, "pullback_zone"),
        _field(intelligence, "ob_zone"),
        _field(intelligence, "fvg_zone"),
        _field(intelligence, "fib_zone"),
        _field(intelligence, "zone"),
        diagnostics.get("entry_zone"),
        diagnostics.get("limit_zone"),
        diagnostics.get("pullback_zone"),
        diagnostics.get("ob_zone"),
        diagnostics.get("fvg_zone"),
        diagnostics.get("fib_zone"),
        diagnostics.get("zone"),
        diagnostics.get("watch_zone"),
        _field(setup, "entry_zone"),
        _field(setup, "limit_zone"),
        _field(setup, "pullback_zone"),
        _field(setup, "ob_zone"),
        _field(setup, "fvg_zone"),
        _field(setup, "fib_zone"),
        _field(setup, "zone"),
        _field(setup, "watch_zone"),
    )
    if _decimal_pair_values(entry_low, entry_high) is None and parsed_zone is not None:
        entry_low, entry_high = parsed_zone
    invalidation_level = _first_non_na(
        _field(intelligence, "invalidation_level"),
        _field(intelligence, "invalidation"),
        _field(intelligence, "invalid_below"),
        _field(intelligence, "invalid_above"),
        _field(intelligence, "atr_stop"),
        _field(intelligence, "protective_stop"),
        diagnostics.get("invalidation_level"),
        diagnostics.get("invalid_below"),
        diagnostics.get("invalid_above"),
        diagnostics.get("atr_stop"),
        diagnostics.get("protective_stop"),
        _field(setup, "invalidation_level"),
        _field(setup, "invalid_below"),
        _field(setup, "invalid_above"),
        _field(setup, "atr_stop"),
        _field(setup, "protective_stop"),
        getattr(lifecycle, "invalidation_level", NA) if lifecycle is not None else NA,
        diagnostics.get("invalidation"),
        _field(setup, "invalidation"),
        getattr(lifecycle, "invalidation_reason", NA) if lifecycle is not None else NA,
        getattr(lifecycle, "invalidation_logic", NA) if lifecycle is not None else NA,
    )
    stop_loss = _first_non_na(
        getattr(lifecycle, "stop_loss", NA) if lifecycle is not None else NA,
        _field(intelligence, "stop_loss"),
        _field(intelligence, "protective_stop"),
        _field(intelligence, "atr_stop"),
        diagnostics.get("stop_loss"),
        diagnostics.get("stop"),
        diagnostics.get("stop_price"),
        diagnostics.get("protective_stop"),
        diagnostics.get("atr_stop"),
        _field(setup, "stop_loss"),
        _field(setup, "stop"),
        _field(setup, "stop_price"),
        _field(setup, "protective_stop"),
        _field(setup, "atr_stop"),
        _level_field(getattr(trade_idea, "stop_loss", None), "price"),
        invalidation_level if _decimal_or_none(invalidation_level) is not None else NA,
    )
    tp1 = _first_non_na(
        getattr(lifecycle, "tp1", NA) if lifecycle is not None else NA,
        _field(intelligence, "tp1"),
        _field(intelligence, "target_1"),
        _field(intelligence, "take_profit_1"),
        diagnostics.get("tp1"),
        diagnostics.get("target_1"),
        diagnostics.get("take_profit_1"),
        diagnostics.get("first_target"),
        _field(setup, "tp1"),
        _field(setup, "target_1"),
        _field(setup, "take_profit_1"),
        _take_profit(trade_idea, 1),
    )
    potential_rr = _first_non_na(
        _field(intelligence, "potential_rr"),
        _field(intelligence, "rr"),
        _field(intelligence, "rr_to_tp1"),
        _field(intelligence, "rr_to_tp2"),
        _field(intelligence, "planned_rr"),
        _field(intelligence, "rr_planned"),
        diagnostics.get("potential_rr"),
        diagnostics.get("public_watchlist_rr"),
        diagnostics.get("rr"),
        diagnostics.get("rr_to_tp1"),
        getattr(lifecycle, "rr", NA) if lifecycle is not None else NA,
        _field(setup, "rr"),
        _field(setup, "potential_rr"),
        _field(setup, "rr_to_tp1"),
        _field(setup, "rr_to_tp2"),
        diagnostics.get("rr_to_tp2"),
        diagnostics.get("planned_rr"),
        diagnostics.get("rr_planned"),
        _field(setup, "planned_rr"),
        _field(setup, "rr_planned"),
        getattr(trade_idea, "best_rr", NA) if trade_idea is not None else NA,
    )
    failed_gate = _failed_gate_code(
        _first_non_na(
            _field(intelligence, "primary_failed_gate"),
            _field(intelligence, "failed_gate"),
            diagnostics.get("first_failed_gate"),
            diagnostics.get("failed_gate"),
            getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
        )
    )
    lifecycle_state = _status_key(_public_gate_state(symbol_result))
    pending_confirmation = _first_non_na(
        _field(intelligence, "pending_reason"),
        _field(intelligence, "pending_confirmation_reason"),
        _field(intelligence, "action_label"),
        _field(intelligence, "watchlist_status"),
        diagnostics.get("pending_confirmation_reason"),
        diagnostics.get("pending_reason"),
        diagnostics.get("confirmation_needed"),
        diagnostics.get("next_trigger_needed"),
        _confirmation_needed(diagnostics),
        failed_gate,
    )
    candidate = PublicWatchlistCandidate(
        symbol=_symbol(symbol_result.symbol),
        side=side if side in {"long", "short"} else NA,
        mode=_text(mode),
        strategy=_text(strategy),
        watchlist_grade=watchlist_grade,
        quality_grade_current=quality_grade_current,
        grade=_text(grade),
        readiness_label=_text(
            _first_non_na(
                _field(intelligence, "readiness_label"),
                _field(intelligence, "readiness_state"),
                diagnostics.get("readiness_label"),
                diagnostics.get("readiness_state"),
            )
        ),
        readiness_score=_text(readiness_score),
        potential_rr=potential_rr,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        stop_loss=stop_loss,
        tp1=tp1,
        invalidation_level=invalidation_level,
        pending_confirmation_reason=_text(pending_confirmation),
        failed_gate=failed_gate or NA,
        lifecycle_state=lifecycle_state or NA,
    )
    return replace(
        candidate,
        plan_complete=_public_watchlist_candidate_plan_complete(candidate),
        first_seen_triggered_pre_confirmation=_is_first_seen_triggered_pre_confirmation(candidate, symbol_result),
    )


def _message_with_public_watchlist_candidate(
    message: TelegramSignalMessage,
    candidate: PublicWatchlistCandidate,
) -> TelegramSignalMessage:
    invalidation = _first_non_na(
        candidate.invalidation_level,
        candidate.stop_loss,
        message.watchlist_invalidation_reason,
        message.invalidation_reason,
    )
    return replace(
        message,
        symbol=candidate.symbol if candidate.symbol != NA else message.symbol,
        direction=candidate.side if candidate.side != NA else message.direction,
        mode=candidate.mode if candidate.mode != NA else message.mode,
        quality=candidate.grade if candidate.grade != NA else message.quality,
        entry_low=candidate.entry_zone_low if _text(candidate.entry_zone_low) != NA else message.entry_low,
        entry_high=candidate.entry_zone_high if _text(candidate.entry_zone_high) != NA else message.entry_high,
        watch_zone=_first_non_na(
            _entry_zone_text(candidate.entry_zone_low, candidate.entry_zone_high),
            message.watch_zone,
        ),
        stop_loss=candidate.stop_loss if _text(candidate.stop_loss) != NA else message.stop_loss,
        tp1=candidate.tp1 if _text(candidate.tp1) != NA else message.tp1,
        planned_rr=candidate.potential_rr if _text(candidate.potential_rr) != NA else message.planned_rr,
        confirmation_needed=(
            candidate.pending_confirmation_reason
            if candidate.pending_confirmation_reason != NA
            else message.confirmation_needed
        ),
        watchlist_invalidation_reason=_as_watchlist_invalidation(invalidation),
        watchlist_status=(
            "LIMIT_ZONE_HIT_WAITING_CONFIRMATION"
            if candidate.first_seen_triggered_pre_confirmation
            else message.watchlist_status
        ),
    )


def _message_with_signal_context(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    message: TelegramSignalMessage,
    *,
    candidate: PublicWatchlistCandidate | None = None,
) -> TelegramSignalMessage:
    context = _signal_message_context_from_symbol(symbol_result, diagnostics, message, candidate=candidate)
    return replace(
        message,
        primary_mode=context.primary_mode,
        secondary_modes=context.secondary_modes,
        source_modes=context.source_modes,
        quality_score=context.quality_score,
        lifecycle_state=context.lifecycle_state,
        technical_score=context.technical_score,
        opportunity_score=context.opportunity_score,
        target_integrity_status=context.target_integrity_status,
        target_failure=context.target_failure,
        target_failure_severity=context.target_failure_severity,
        target_warning_reason=context.target_warning_reason,
        final_failed_gate=context.final_failed_gate,
        final_block_reason=context.final_block_reason,
        invalidation_logic=context.invalidation_logic,
        why_it_matters_points=context.why_it_matters_points,
        what_we_want_next_points=context.what_we_want_next_points,
        caution_points=context.caution_points,
        signal_context=context,
    )


def _signal_message_context_from_symbol(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    message: TelegramSignalMessage,
    *,
    candidate: PublicWatchlistCandidate | None = None,
) -> SignalMessageContext:
    lifecycle = symbol_result.lifecycle_state
    source_modes = _signal_context_source_modes(symbol_result, diagnostics, message, candidate=candidate)
    primary_mode = _signal_context_primary_mode(symbol_result, diagnostics, message, source_modes, candidate=candidate)
    secondary_modes = tuple(mode for mode in source_modes if mode != primary_mode)
    valid_modes = set(_signal_mode_values(symbol_result.valid_strategy_modes))
    actionability_state = _first_non_na(
        getattr(symbol_result, "actionability_state", NA),
        getattr(lifecycle, "actionability_state", NA) if lifecycle is not None else NA,
        message.actionability_state,
        diagnostics.get("actionability_state"),
    )
    target_warning_reason = _first_non_na(
        getattr(symbol_result, "target_warning_reason", NA),
        getattr(lifecycle, "target_warning_reason", NA) if lifecycle is not None else NA,
        diagnostics.get("target_warning_reason"),
        diagnostics.get("target_integrity_warning"),
        diagnostics.get("target_integrity_reason"),
        message.target_warning_reason,
    )
    target_failure_severity = _first_non_na(
        getattr(symbol_result, "target_failure_severity", NA),
        getattr(lifecycle, "target_failure_severity", NA) if lifecycle is not None else NA,
        diagnostics.get("target_failure_severity"),
        message.target_failure_severity,
    )
    final_failed_gate = _first_non_na(
        getattr(symbol_result, "final_failed_gate", NA),
        getattr(lifecycle, "final_failed_gate", NA) if lifecycle is not None else NA,
        diagnostics.get("first_failed_gate"),
        diagnostics.get("failed_gate"),
        getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
        message.final_failed_gate,
    )
    final_block_reason = _first_non_na(
        getattr(symbol_result, "final_block_reason", NA),
        getattr(lifecycle, "final_block_reason", NA) if lifecycle is not None else NA,
        diagnostics.get("final_block_reason"),
        diagnostics.get("block_reason"),
        message.final_block_reason,
    )
    context = SignalMessageContext(
        symbol=symbol_result.symbol,
        direction=message.direction,
        primary_mode=primary_mode,
        secondary_modes=secondary_modes,
        source_modes=source_modes,
        confluence_valid={"scalp", "swing"}.issubset(valid_modes),
        grade=message.quality,
        quality_score=_signal_context_quality_score(symbol_result, diagnostics, message),
        lifecycle_state=_signal_context_lifecycle_state(symbol_result, candidate),
        actionability_state=actionability_state,
        entry_low=message.entry_low,
        entry_high=message.entry_high,
        stop_loss=message.stop_loss,
        tp1=message.tp1,
        tp2=message.tp2,
        tp3=message.tp3,
        rr=message.planned_rr,
        technical_score=_first_non_na(
            getattr(lifecycle, "technical_score", NA) if lifecycle is not None else NA,
            symbol_result.technical_score,
            diagnostics.get("technical_score"),
            message.technical_score,
        ),
        opportunity_score=_first_non_na(
            getattr(lifecycle, "opportunity_score", NA) if lifecycle is not None else NA,
            _opportunity_score_text(symbol_result),
            diagnostics.get("opportunity_score"),
            diagnostics.get("total_score"),
            message.opportunity_score,
        ),
        target_integrity_status=_first_non_na(
            getattr(symbol_result, "target_integrity_status", NA),
            getattr(lifecycle, "target_integrity_status", NA) if lifecycle is not None else NA,
            diagnostics.get("target_integrity_status"),
            message.target_integrity_status,
        ),
        target_failure=_first_non_na(
            getattr(symbol_result, "target_failure", NA),
            getattr(lifecycle, "target_failure", NA) if lifecycle is not None else NA,
            diagnostics.get("target_failure"),
            diagnostics.get("target_failure_type"),
            message.target_failure,
        ),
        target_failure_severity=target_failure_severity,
        target_warning_reason=target_warning_reason,
        final_failed_gate=final_failed_gate,
        final_block_reason=final_block_reason,
        invalidation_logic=_signal_context_invalidation_logic(message, diagnostics),
        why_it_matters_points=(),
        what_we_want_next_points=(),
        caution_points=(),
    )
    return replace(
        context,
        why_it_matters_points=_signal_context_why_points(symbol_result, diagnostics, context),
        what_we_want_next_points=_signal_context_next_points(context, diagnostics),
        caution_points=_signal_context_caution_points(context),
    )


def _signal_context_source_modes(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    message: TelegramSignalMessage,
    *,
    candidate: PublicWatchlistCandidate | None,
) -> tuple[str, ...]:
    values: list[Any] = [
        symbol_result.valid_strategy_modes,
        getattr(symbol_result.lifecycle_state, "mode", NA),
        candidate.mode if candidate is not None else NA,
        diagnostics.get("source_modes"),
        diagnostics.get("mode"),
        message.source_modes,
        message.mode,
    ]
    if not _signal_mode_values(symbol_result.valid_strategy_modes):
        values.append(symbol_result.rejected_strategy_modes)
    return _unique_signal_modes(*values)


def _signal_context_primary_mode(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    message: TelegramSignalMessage,
    source_modes: Sequence[str],
    *,
    candidate: PublicWatchlistCandidate | None,
) -> str:
    for value in (
        message.primary_mode,
        message.mode,
        getattr(symbol_result.lifecycle_state, "mode", NA),
        candidate.mode if candidate is not None else NA,
        diagnostics.get("selected_mode"),
        diagnostics.get("mode"),
    ):
        modes = _signal_mode_values(value)
        if modes:
            return modes[0]
    return source_modes[0] if source_modes else NA


def _unique_signal_modes(*values: Any) -> tuple[str, ...]:
    modes: list[str] = []
    for value in values:
        for mode in _signal_mode_values(value):
            if mode not in modes:
                modes.append(mode)
    return tuple(modes)


def _signal_mode_values(value: Any) -> tuple[str, ...]:
    if value is None or value == NA or isinstance(value, Mapping):
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _unique_signal_modes(*value)
    text = _text(value)
    if text == NA:
        return ()
    key = text.lower().replace("+", " ").replace(",", " ").replace("/", " ").replace("|", " ")
    key = key.replace("-", "_")
    tokens: list[str] = []
    for mode in ("scalp", "swing", "challenge"):
        parts = key.split()
        if key == mode or mode in parts or key.endswith(f"_{mode}") or f"_{mode}_" in key:
            tokens.append(mode)
    return tuple(tokens)


def _signal_context_quality_score(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    message: TelegramSignalMessage,
) -> Any:
    quality = symbol_result.setup_quality
    return _first_non_zero_text(
        message.quality_score,
        diagnostics.get("setup_quality_score"),
        diagnostics.get("quality_score"),
        getattr(quality, "quality_score", NA) if getattr(quality, "is_evaluated", False) else NA,
        getattr(symbol_result.lifecycle_state, "quality_score", NA) if symbol_result.lifecycle_state is not None else NA,
        diagnostics.get("readiness_score"),
    )


def _first_non_zero_text(*values: Any) -> Any:
    for value in values:
        text = _text(value)
        if text != NA and text != "0":
            return value
    return NA


def _signal_context_lifecycle_state(
    symbol_result: ScannerSymbolResult,
    candidate: PublicWatchlistCandidate | None,
) -> str:
    lifecycle = symbol_result.lifecycle_state
    return _first_non_na(
        getattr(getattr(lifecycle, "current_state", NA), "value", getattr(lifecycle, "current_state", NA)) if lifecycle is not None else NA,
        candidate.lifecycle_state if candidate is not None else NA,
    )


def _signal_context_invalidation_logic(message: TelegramSignalMessage, diagnostics: Mapping[str, Any]) -> str:
    stop = format_telegram_price(message.stop_loss)
    if stop == NA:
        return "Hard invalidation: stop level unavailable in stored context."
    direction = _status_key(message.direction)
    side_word = "below" if direction == "long" else "above" if direction == "short" else "beyond"
    body_context = _text(diagnostics.get("body_acceptance_ratio")) != NA or _text(diagnostics.get("acceptance_status")) != NA
    close_language = "body-closes and accepts" if body_context else "accepts"
    return f"Invalid if price {close_language} {side_word} {stop}."


def _signal_context_why_points(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    context: SignalMessageContext,
) -> tuple[str, ...]:
    passed_tokens = _context_status_tokens(
        diagnostics.get("gates_passed"),
        diagnostics.get("passed_gates"),
        diagnostics.get("completed_gates"),
    )
    points: list[str] = []
    sweep = _context_contains_any(passed_tokens, ("sweep", "liquidity_sweep")) or _context_any_positive(
        diagnostics,
        "liquidity_sweep_detected",
        "sweep_detected",
        "execution_sweep_detected",
        "execution_sweep_status",
        "sweep_status",
    ) or bool(getattr(symbol_result, "sweep_detected", False))
    if sweep:
        sweep_label = _signal_context_sweep_label(context, diagnostics)
        if sweep_label != NA:
            points.append(f"{sweep_label} liquidity was swept before the setup mapped.")
        else:
            points.append("Liquidity sweep is present in diagnostics.")

    reclaim = _context_contains_any(passed_tokens, ("reclaim", "wick_reclaim")) or _context_any_positive(
        diagnostics,
        "reclaim_detected",
        "wick_reclaim_detected",
        "wick_reclaim_status",
        "reclaim_status",
        "structural_reclaim_status",
    )
    if reclaim:
        points.append("Price reclaimed the swept zone instead of accepting through it.")

    structure_shift = _context_contains_any(passed_tokens, ("bos", "choch", "mss", "structure_shift")) or _context_any_positive(
        diagnostics,
        "bos_detected",
        "choch_detected",
        "mss_detected",
        "structure_shift_detected",
        "confirmation_structure_shift_status",
        "bos_choch_status",
    ) or bool(getattr(symbol_result, "bos_detected", False)) or bool(getattr(symbol_result, "choch_detected", False))
    if structure_shift:
        points.append("5m BOS/CHoCH confirms the structure shift.")

    zone_type = _status_key(_first_non_na(diagnostics.get("selected_zone_type"), diagnostics.get("ob_fvg_status")))
    has_ob = "ob" in zone_type or "order_block" in zone_type or _context_contains_any(passed_tokens, ("ob", "order_block"))
    has_fvg = "fvg" in zone_type or "imbalance" in zone_type or _context_contains_any(passed_tokens, ("fvg", "imbalance"))
    if has_ob and has_fvg:
        points.append("Pullback is mapped into an OB/FVG reaction zone.")
    elif has_ob:
        points.append("Pullback is mapped into an order-block reaction zone.")
    elif has_fvg:
        points.append("Pullback is mapped into an FVG reaction zone.")

    fib_status = _status_key(diagnostics.get("fib_alignment_status"))
    if _context_contains_any(passed_tokens, ("fib",)) or fib_status in {"aligned", "valid", "passed", "confirmed"}:
        points.append("Pullback depth aligns with the fib pocket.")

    target_status = _status_key(context.target_integrity_status)
    warning_key = _status_key(context.target_warning_reason)
    if target_status in {"passed", "valid", "clean"}:
        points.append("Target integrity leaves a clean RR path toward TP2.")
    elif target_status == "warning" or "chop" in warning_key or "range" in warning_key:
        points.append("Target path is tighter/choppy, so TP1 reaction matters.")

    regime_point = _signal_context_regime_point(symbol_result, diagnostics)
    if regime_point != NA:
        points.append(regime_point)

    return tuple(_dedupe_public_points(points))


def _signal_context_sweep_label(context: SignalMessageContext, diagnostics: Mapping[str, Any]) -> str:
    label = _sweep_direction_label(diagnostics)
    if label != NA:
        return label
    direction = _status_key(context.direction)
    if direction == "long":
        return "Downside"
    if direction == "short":
        return "Upside"
    return NA


def _signal_context_regime_point(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    label = _status_key(
        _first_non_na(
            symbol_result.regime_compatibility_label,
            diagnostics.get("regime_compatibility_label"),
            _mapping_value(symbol_result.regime_diagnostics, "compatibility_label"),
        )
    )
    if not label or "acceptable" in label or "neutral" in label:
        return NA
    if any(fragment in label for fragment in ("support", "favorable", "strong", "allowed", "tailwind")):
        state = _first_non_na(symbol_result.regime_state, diagnostics.get("regime_state"), _mapping_value(symbol_result.regime_diagnostics, "state"))
        state_text = _plain_label(state) if _text(state) != NA else "market"
        return f"Regime context supports the setup in the {state_text} environment."
    return NA


def _signal_context_next_points(context: SignalMessageContext, diagnostics: Mapping[str, Any]) -> tuple[str, ...]:
    actionability = _status_key(context.actionability_state)
    lifecycle = _status_key(context.lifecycle_state)
    failed_gate = _status_key(context.final_failed_gate)
    warning = _clean_public_sentence(context.target_warning_reason)
    points: list[str] = []
    if actionability == "a_grade_actionable_target_caution":
        points.append("No chase. We want clean entry reaction and fast movement away from chop.")
        points.append("TP1 reaction matters; TP2 path is tighter/choppy.")
    elif actionability in {"a_grade_actionable", "actionable_a_grade"} or lifecycle == "actionable_a_grade":
        points.append("Hold entry zone and continue displacement toward TP1.")
    elif lifecycle in {"watch", "watchlisted", "stalking"}:
        points.append("Wait for 5m BOS/CHoCH confirmation after sweep.")
    elif lifecycle == "triggered":
        points.append("Need confirmation candle close and target path expansion.")

    actionable_state = actionability in {"a_grade_actionable", "a_grade_actionable_target_caution", "actionable_a_grade"} or lifecycle == "actionable_a_grade"
    gate_point = _signal_context_gate_next_point(failed_gate)
    if gate_point != NA and not actionable_state:
        points.append(gate_point)
    if warning != NA and (actionability == "a_grade_actionable_target_caution" or _status_key(context.target_integrity_status) == "warning"):
        points.append(warning)
    if not points:
        text = _clean_public_sentence(_first_non_na(diagnostics.get("next_trigger_needed"), diagnostics.get("confirmation_needed")))
        if text != NA:
            points.append(text)
    return tuple(_dedupe_public_points(points))


def _signal_context_gate_next_point(failed_gate: str) -> str:
    if not failed_gate:
        return NA
    if "limit_zone" in failed_gate or "entry_zone" in failed_gate or "pullback" in failed_gate:
        return "Wait for pullback into entry zone. No market chase."
    if "confirmation" in failed_gate or "bos" in failed_gate or "choch" in failed_gate:
        return "Wait for 5m BOS/CHoCH confirmation after sweep."
    if "target" in failed_gate or "rr" in failed_gate:
        return "Need target path expansion before treating the setup as clean."
    return NA


def _signal_context_caution_points(context: SignalMessageContext) -> tuple[str, ...]:
    actionability = _status_key(context.actionability_state)
    severity = _status_key(context.target_failure_severity)
    warning = _clean_public_sentence(context.target_warning_reason)
    warning_key = _status_key(warning)
    if not (
        actionability == "a_grade_actionable_target_caution"
        or severity in {"target_caution_actionable", "soft_target_warning"}
        or "chop" in warning_key
        or "range" in warning_key
    ):
        return ()
    warning_detail = () if warning == NA else (warning,)
    return _dedupe_public_points(
        (
            *warning_detail,
            "Target path is tighter/choppy - no chase.",
            "TP1 reaction matters.",
            "Reduce aggression until price clears chop.",
        )
    )


def _dedupe_public_points(points: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for point in points:
        text = _clean_public_sentence(point)
        if text == NA:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return tuple(output)

def _public_watchlist_candidate_plan_complete(candidate: PublicWatchlistCandidate) -> bool:
    return not _public_watchlist_candidate_missing_fields(candidate)


def _public_watchlist_candidate_missing_fields(candidate: PublicWatchlistCandidate) -> tuple[str, ...]:
    missing: list[str] = []
    if candidate.symbol == NA:
        missing.append("missing_symbol")
    if candidate.side not in {"long", "short"}:
        missing.append("missing_side")
    if _text(candidate.grade) == NA:
        missing.append("missing_grade")
    if _decimal_or_none(candidate.potential_rr) is None:
        missing.append("missing_rr")
    if _decimal_pair_values(candidate.entry_zone_low, candidate.entry_zone_high) is None:
        missing.append("missing_entry_zone")
    if _decimal_or_none(candidate.stop_loss) is None:
        missing.append("missing_stop")
    if _decimal_or_none(candidate.tp1) is None:
        missing.append("missing_tp1")
    if not _public_watchlist_candidate_has_invalidation(candidate):
        missing.append("missing_invalidation")
    return tuple(missing)


def _public_watchlist_candidate_precise_missing_blockers(candidate: PublicWatchlistCandidate) -> tuple[str, ...]:
    precise = {
        "missing_symbol": "public_watchlist_missing_symbol",
        "missing_side": "public_watchlist_missing_side",
        "missing_grade": "public_watchlist_missing_grade",
        "missing_rr": "public_watchlist_missing_rr",
        "missing_entry_zone": "public_watchlist_missing_entry_zone",
        "missing_stop": "public_watchlist_missing_stop",
        "missing_tp1": "public_watchlist_missing_tp1",
        "missing_invalidation": "public_watchlist_missing_invalidation",
    }
    return tuple(precise[field] for field in _public_watchlist_candidate_missing_fields(candidate) if field in precise)


def _public_watchlist_candidate_has_invalidation(candidate: PublicWatchlistCandidate) -> bool:
    return _text(_first_non_na(candidate.stop_loss, candidate.invalidation_level)) != NA


def _public_watchlist_candidate_has_zone(candidate: PublicWatchlistCandidate) -> bool:
    return _decimal_pair_values(candidate.entry_zone_low, candidate.entry_zone_high) is not None


def _is_first_seen_triggered_pre_confirmation(
    candidate: PublicWatchlistCandidate,
    symbol_result: ScannerSymbolResult,
) -> bool:
    if candidate.lifecycle_state != PUBLIC_WATCHLIST_FIRST_SEEN_TRIGGERED_STATE_KEY:
        return False
    if candidate.lifecycle_state in PUBLIC_WATCHLIST_BLOCKED_STATE_KEYS:
        return False
    if candidate.failed_gate in {
        "confirmed",
        "confirmation_complete",
        "executing",
        "invalidated",
        "expired",
        "cooldown",
    }:
        return False
    if _status_key(getattr(symbol_result.status, "value", symbol_result.status)) in {"confirmed", "executing"}:
        return False
    return True


def _first_normalized_grade(*values: Any) -> str:
    for value in values:
        grade = normalize_grade(value)
        if grade != NA:
            return grade
    return NA


def _first_non_reject_normalized_grade(*values: Any) -> str:
    for value in values:
        grade = normalize_grade(value)
        if grade != NA and _status_key(grade) not in {"reject", "rejected", "no_trade"}:
            return grade
    return NA


def _first_decimal_pair_text(*values: Any) -> tuple[Decimal, Decimal] | None:
    for value in values:
        pair = _decimal_pair_text(value)
        if pair is not None:
            return pair
    return None


def _compose_simple_signal_reason(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    message: TelegramSignalMessage,
    candidate: PublicWatchlistCandidate | None = None,
) -> str:
    candidate = candidate or _public_watchlist_candidate_from_symbol(symbol_result)
    passed_tokens = _context_status_tokens(
        diagnostics.get("gates_passed"),
        diagnostics.get("passed_gates"),
        diagnostics.get("completed_gates"),
    )
    failed_codes = set(_public_watchlist_failed_gate_codes(symbol_result, candidate=candidate))
    context_tokens = _context_status_tokens(
        diagnostics.get("first_failed_gate"),
        diagnostics.get("failed_gate"),
        diagnostics.get("pending_reason"),
        diagnostics.get("pending_confirmation_reason"),
        diagnostics.get("next_required_conditions"),
        diagnostics.get("confirmation_needed"),
        message.confirmation_needed,
        message.watchlist_status,
        candidate.pending_confirmation_reason,
    )
    sweep = _context_contains_any(passed_tokens, ("sweep", "liquidity_sweep")) or _context_any_positive(
        diagnostics,
        "liquidity_sweep_detected",
        "sweep_detected",
        "execution_sweep_detected",
        "execution_sweep_status",
        "sweep_status",
    )
    reclaim = _context_contains_any(passed_tokens, ("reclaim", "wick_reclaim")) or _context_any_positive(
        diagnostics,
        "reclaim_detected",
        "wick_reclaim_detected",
        "wick_reclaim_status",
        "reclaim_status",
        "structural_reclaim_status",
    )
    structure_shift = _context_contains_any(passed_tokens, ("bos", "choch", "mss", "structure_shift")) or _context_any_positive(
        diagnostics,
        "bos_detected",
        "choch_detected",
        "mss_detected",
        "structure_shift_detected",
        "confirmation_structure_shift_status",
        "bos_choch_status",
    )
    waiting_pullback = bool(
        failed_codes
        & {
            "limit_zone_not_hit",
            "limit_zone_not_touched",
            "entry_zone_not_hit",
            "entry_zone_not_touched",
            "pullback_pending",
            "fvg_not_tapped",
            "ob_retest_pending",
            "limit_zone_hold_pending",
            "pullback_hold_pending",
        }
    ) or _context_contains_any(context_tokens, ("pullback", "limit_zone", "entry_zone", "ob_retest", "fvg_not_tapped"))
    waiting_confirmation = any("confirmation" in code or "trigger" in code for code in failed_codes) or _context_contains_any(
        context_tokens,
        ("confirmation", "trigger_not_hit", "stalking_not_triggered", "bos_body_close_pending", "choch_confirmation_pending"),
    )
    order_block = _context_contains_any(passed_tokens, ("order_block", "ob", "ob_fvg")) or _diagnostic_text_contains(
        diagnostics,
        "order_block",
        "ob",
        "selected_zone_type",
        "ob_fvg_status",
        "ob_fvg_diagnostics",
    )
    fvg = _context_contains_any(passed_tokens, ("fvg", "imbalance", "ob_fvg")) or _diagnostic_text_contains(
        diagnostics,
        "fvg",
        "imbalance",
        "selected_zone_type",
        "ob_fvg_status",
        "ob_fvg_diagnostics",
    )
    regime_pending = any(classify_failed_gate_code(code) == REGIME_MARKET_CONDITION_PENDING for code in failed_codes)
    side_group = _simple_signal_side_group(message.direction)
    sweep_label = _sweep_direction_label(diagnostics)
    sweep_subject = f"{sweep_label} liquidity" if sweep_label != NA else "Liquidity"

    if sweep and structure_shift and waiting_pullback:
        if reclaim:
            return "Liquidity sweep, reclaim, and structure shift are already in place. The setup is now waiting for price to pull back into the planned limit entry zone."
        return "Liquidity sweep and structure shift are already in place. The setup is now waiting for price to pull back into the planned limit entry zone."
    if sweep and reclaim and structure_shift:
        return f"{sweep_subject} was swept, price reclaimed the zone, and structure is starting to shift back in favor of {side_group}."
    if sweep and reclaim:
        return "Liquidity was swept and price reclaimed the zone, giving a defined manual setup with clear invalidation."
    if sweep and waiting_confirmation:
        return "Liquidity has been swept, but structure has not fully confirmed yet. This is a stalking setup - confirmation is still required before aggressive execution."
    if order_block or fvg:
        if order_block and fvg:
            zone_text = "validated imbalance/order-block zone"
        elif order_block:
            zone_text = "validated order-block zone"
        else:
            zone_text = "validated imbalance zone"
        return f"Price is reacting around a {zone_text}, giving a defined entry area with clear invalidation."
    if regime_pending:
        return "Structure and trade map are valid, but broader market conditions are not fully supportive. This remains a manual-execution setup, not an automatic confirmation."
    if waiting_confirmation:
        return "The trade map is valid, but confirmation is still pending. This remains a manual-execution setup, not an automatic confirmation."
    if waiting_pullback:
        return "The trade map is defined and price is waiting for a pullback into the planned limit entry zone before manual execution."
    existing = _clean_public_sentence(message.structure_reason)
    if existing != NA and not _looks_raw_or_generic_context(existing):
        return existing
    return "The setup has a complete trade map, acceptable quality, and defined invalidation. Treat it as a manual signal only."


def _context_status_tokens(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for item in _sequence_or_single(value):
            key = _status_key(item)
            if key:
                tokens.add(key)
    return tokens


def _context_contains_any(tokens: set[str], fragments: Sequence[str]) -> bool:
    return any(fragment in token for token in tokens for fragment in fragments)


def _context_any_positive(diagnostics: Mapping[str, Any], *keys: str) -> bool:
    return any(_context_positive(diagnostics.get(key)) for key in keys)


def _context_positive(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    key = _status_key(value)
    return key in {
        "1",
        "true",
        "yes",
        "y",
        "pass",
        "passed",
        "detected",
        "confirmed",
        "present",
        "valid",
        "validated",
        "complete",
        "completed",
        "intact",
    }


def _diagnostic_text_contains(diagnostics: Mapping[str, Any], *fragments: str) -> bool:
    for key, value in diagnostics.items():
        if key not in fragments:
            continue
        text = _text(value).lower()
        if text != NA.lower() and any(fragment in text for fragment in fragments):
            return True
    return False


def _simple_signal_side_group(direction: Any) -> str:
    side = _status_key(direction)
    if side == "long":
        return "longs"
    if side == "short":
        return "shorts"
    return "the planned side"


def _sweep_direction_label(diagnostics: Mapping[str, Any]) -> str:
    key = _status_key(
        _first_non_na(
            diagnostics.get("sweep_direction"),
            diagnostics.get("liquidity_sweep_direction"),
            diagnostics.get("swept_liquidity_side"),
        )
    )
    if key in {"down", "downside", "sell_side", "sellside", "below", "lows"}:
        return "Downside"
    if key in {"up", "upside", "buy_side", "buyside", "above", "highs"}:
        return "Upside"
    return NA

def _telegram_signal_message_for_alert(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    context: TelegramEligibilityContext,
) -> TelegramSignalMessage:
    message = replace(telegram_signal_message_from_symbol(symbol_result), min_rr=context.min_rr)
    if alert_type == TelegramAlertType.WATCHLIST:
        diagnostics = _representative_diagnostics(symbol_result)
        candidate = _public_watchlist_candidate_from_symbol(symbol_result)
        message = _message_with_public_watchlist_candidate(message, candidate)
        message = replace(
            message,
            min_rr=context.public_watchlist_min_rr,
            regime_state=_first_non_na(
                symbol_result.regime_state,
                diagnostics.get("regime_state"),
                _mapping_value(symbol_result.regime_diagnostics, "state"),
            ),
            regime_compatibility_label=_first_non_na(
                symbol_result.regime_compatibility_label,
                diagnostics.get("regime_compatibility_label"),
                _mapping_value(symbol_result.regime_diagnostics, "compatibility_label"),
            ),
            regime_confidence=_first_non_na(
                symbol_result.regime_confidence_score,
                diagnostics.get("regime_confidence"),
                diagnostics.get("regime_confidence_score"),
                _mapping_value(symbol_result.regime_diagnostics, "confidence_score"),
            ),
        )
        message = replace(
            message,
            structure_reason=_compose_simple_signal_reason(symbol_result, diagnostics, message, candidate),
        )
        message = _message_with_signal_context(symbol_result, diagnostics, message, candidate=candidate)
    if alert_type in TERMINAL_UPDATE_ALERT_TYPES:
        return replace(message, invalidation_reason=_terminal_update_reason(symbol_result, alert_type))
    return message


def format_research_watch_alert(candidate: ResearchWatchCandidate) -> str:
    return format_telegram_signal_message(TelegramAlertType.RESEARCH_WATCH, candidate.message)


def _research_watch_candidate(
    symbol_result: ScannerSymbolResult,
    eligibility: ResearchWatchEligibilityConfig,
) -> ResearchWatchCandidate | None:
    display = build_symbol_display(symbol_result)
    diagnostics = _representative_diagnostics(symbol_result)
    quality_score = _integer_or_zero(getattr(symbol_result.setup_quality, "quality_score", NA))
    readiness_score = _integer_or_zero(display.readiness_score)
    next_trigger = _first_non_na(
        diagnostics.get("next_trigger_needed"),
        display.next_trigger_needed,
        diagnostics.get("confirmation_needed"),
    )
    action_label = _first_non_na(
        diagnostics.get("action_label"),
        getattr(symbol_result.setup_quality, "action_label", NA),
        display.action_label,
    )
    regime_state = _first_non_na(
        symbol_result.regime_state,
        diagnostics.get("regime_state"),
        _mapping_value(symbol_result.regime_diagnostics, "state"),
    )
    regime_compatibility_label = _first_non_na(
        symbol_result.regime_compatibility_label,
        diagnostics.get("regime_compatibility_label"),
        _mapping_value(symbol_result.regime_diagnostics, "compatibility_label"),
    )
    regime_confidence = _first_non_na(
        symbol_result.regime_confidence_score,
        diagnostics.get("regime_confidence"),
        diagnostics.get("regime_confidence_score"),
        _mapping_value(symbol_result.regime_diagnostics, "confidence_score"),
    )
    rejection_reason = _first_non_na(
        symbol_result.rejection_reason,
        diagnostics.get("rejection_reason"),
        diagnostics.get("regime_compatibility_reason"),
        display.short_reason,
    )
    row = {
        "symbol": symbol_result.symbol,
        "status": getattr(symbol_result.status, "value", symbol_result.status),
        "display_bucket": display.display_bucket,
        "setup_quality_score": quality_score,
        "readiness_score": readiness_score,
        "next_trigger_needed": next_trigger,
        "action_label": action_label,
        "regime_state": regime_state,
        "regime_confidence": regime_confidence,
        "regime_compatibility_label": regime_compatibility_label,
        "regime_blocked": symbol_result.regime_blocked,
        "rejection_reason": rejection_reason,
        "rejection_reasons": symbol_result.rejection_reasons,
        "failed_gate": display.failed_gate,
        "lifecycle_state": symbol_result.lifecycle_state,
    }
    if not research_watch_eligible(row, eligibility):
        return None
    signal_id_prefix = _research_watch_signal_id_prefix(
        symbol_result,
        regime_state=regime_state,
        next_trigger=next_trigger,
        action_label=action_label,
    )
    message = replace(
        telegram_signal_message_from_symbol(symbol_result),
        signal_id=signal_id_prefix,
        quality=quality_score,
        readiness_score=readiness_score,
        regime_state=regime_state,
        regime_compatibility_label=regime_compatibility_label,
        regime_confidence=regime_confidence,
        confirmation_needed=next_trigger,
    )
    return ResearchWatchCandidate(
        symbol_result=symbol_result,
        message=message,
        signal_id_prefix=signal_id_prefix,
        quality_score=quality_score,
        readiness_score=readiness_score,
        regime_state=_text(regime_state),
        regime_compatibility_label=_text(regime_compatibility_label),
        regime_confidence=_text(regime_confidence),
        next_trigger=_text(next_trigger),
        action_label=_text(action_label),
        rejection_reason=_text(rejection_reason),
    )


def _persist_research_watch_attempt(
    repository: SQLiteTelegramAlertAttemptRepository,
    candidate: ResearchWatchCandidate,
    *,
    signal_id: str,
    scan_run_id: str | None,
    attempted_at: str,
    status: str,
    detail: str,
    message_hash: str,
    blocked_reason: str = NA,
    error_message: str = NA,
) -> TelegramLifecycleDelivery:
    message = candidate.message
    symbol_result = candidate.symbol_result
    record = TelegramAlertAttemptRecord(
        signal_id=signal_id,
        symbol=symbol_result.symbol,
        direction=_text(message.direction),
        previous_state=NA,
        new_state=TelegramAlertType.RESEARCH_WATCH.value,
        alert_type=TelegramAlertType.RESEARCH_WATCH.value,
        lifecycle_state=NA,
        sent_at=attempted_at if status == "sent" else None,
        attempted_at=attempted_at,
        telegram_status=status,
        message_hash=message_hash,
        scan_run_id=scan_run_id,
        first_seen_at=attempted_at,
        last_seen_at=attempted_at,
        last_scan_run_id=scan_run_id,
        attempted_alert_type=TelegramAlertType.RESEARCH_WATCH.value,
        setup_quality_score=_text(candidate.quality_score),
        rr_planned=_text(message.planned_rr),
        min_rr=NA,
        opportunity_score=_opportunity_score_text(symbol_result),
        min_score_for_idea=NA,
        technical_score=_technical_score_text(symbol_result),
        price_level=_price_level_for_alert(TelegramAlertType.RESEARCH_WATCH, message),
        **_message_level_metadata(message),
        blocked_reason=_text(blocked_reason),
        invalid_target_fields=NA,
        error_message=_text(error_message),
    )
    inserted = repository.insert_attempt(record)
    if not inserted and status in {"blocked", "skipped"} and repository.compact_repeated_attempt(record):
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=TelegramAlertType.RESEARCH_WATCH.value,
            status=status,
            detail=detail,
            message_hash=message_hash,
            error_message=_text(error_message),
        )
    if not inserted:
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=TelegramAlertType.RESEARCH_WATCH.value,
            status="duplicate",
            detail="Duplicate Research Watch alert prevented.",
            message_hash=message_hash,
            error_message=_text(error_message),
        )
    return TelegramLifecycleDelivery(
        symbol=symbol_result.symbol,
        signal_id=signal_id,
        alert_type=TelegramAlertType.RESEARCH_WATCH.value,
        status=status,
        detail=detail,
        message_hash=message_hash,
        error_message=_text(error_message),
    )


def _research_watch_signal_id_prefix(
    symbol_result: ScannerSymbolResult,
    *,
    regime_state: Any,
    next_trigger: Any,
    action_label: Any,
) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    stable_parts = (
        symbol_result.symbol,
        TelegramAlertType.RESEARCH_WATCH.value,
        regime_state,
        _first_non_na(next_trigger, action_label),
        _research_setup_fingerprint(symbol_result, diagnostics),
    )
    digest = hashlib.sha256("|".join(_text(part) for part in stable_parts).encode("utf-8")).hexdigest()[:20]
    return f"{symbol_result.symbol}-RESEARCH-{digest}"


def _research_watch_signal_id(signal_id_prefix: str, attempted_at: str) -> str:
    digest = hashlib.sha256(f"{signal_id_prefix}|{attempted_at}".encode("utf-8")).hexdigest()[:10]
    return f"{signal_id_prefix}-{digest}"


def _research_cooldown_symbol(value: Any) -> str:
    symbol = _symbol(value)
    if symbol.endswith(".P"):
        return symbol[:-2]
    return symbol


def _research_watch_cooldown_signal_id(symbol: Any) -> str:
    normalized_symbol = _research_cooldown_symbol(symbol)
    digest = hashlib.sha256(
        f"{normalized_symbol}|{TelegramAlertType.RESEARCH_WATCH.value}|cooldown".encode("utf-8")
    ).hexdigest()[:12]
    return f"{normalized_symbol}-RESEARCH-COOLDOWN-{digest}"


def _research_setup_fingerprint(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    setup = _selected_setup(symbol_result, diagnostics)
    return _first_non_na(
        diagnostics.get("setup_fingerprint"),
        diagnostics.get("setup_identity"),
        diagnostics.get("fingerprint"),
        _field(setup, "setup_fingerprint"),
        _field(setup, "setup_identity"),
        _fallback_signal_id(symbol_result),
    )


def _research_watch_cooldown_cutoff(attempted_at: str, cooldown_minutes: int) -> str:
    parsed = _parse_iso_datetime(attempted_at)
    return (parsed - timedelta(minutes=max(0, int(cooldown_minutes)))).isoformat()


def _public_watchlist_cooldown_cutoff(attempted_at: str, cooldown_hours: int) -> str:
    parsed = _parse_iso_datetime(attempted_at)
    return (parsed - timedelta(hours=max(0, int(cooldown_hours)))).isoformat()


def _public_watchlist_cooldown_cutoff_minutes(attempted_at: str, cooldown_minutes: int) -> str:
    parsed = _parse_iso_datetime(attempted_at)
    return (parsed - timedelta(minutes=max(0, int(cooldown_minutes)))).isoformat()


def _public_watchlist_daily_cap_reached(
    repository: SQLiteTelegramAlertAttemptRepository,
    context: TelegramEligibilityContext,
    *,
    attempted_at: str,
) -> bool:
    cap = max(0, int(context.public_watchlist_max_per_24h))
    if cap <= 0:
        return True
    since = _public_watchlist_cooldown_cutoff(attempted_at, PUBLIC_SIGNAL_ROLLING_WINDOW_HOURS)
    return repository.count_recent_public_signal_alerts(since=since) >= cap


def _public_watchlist_hourly_cap_reached(
    repository: SQLiteTelegramAlertAttemptRepository,
    context: TelegramEligibilityContext,
    *,
    attempted_at: str,
) -> bool:
    cap = max(0, int(context.public_watchlist_max_per_60m))
    if cap <= 0:
        return True
    since = _public_watchlist_cooldown_cutoff_minutes(attempted_at, PUBLIC_SIGNAL_ROLLING_WINDOW_MINUTES)
    return repository.count_recent_public_signal_alerts(since=since) >= cap


def _public_watchlist_symbol_cooldown_block_reason(
    repository: SQLiteTelegramAlertAttemptRepository,
    message: TelegramSignalMessage,
    *,
    attempted_at: str,
) -> str | None:
    side = _status_key(message.direction)
    if side not in {"long", "short"}:
        return None
    same_side_since = _public_watchlist_cooldown_cutoff(attempted_at, PUBLIC_SIGNAL_SAME_SIDE_COOLDOWN_HOURS)
    if repository.has_recent_public_signal_for_symbol_direction(
        symbol=message.symbol,
        direction=side,
        since=same_side_since,
    ):
        return "public_block_same_symbol_same_side_cooldown"
    opposite_side = "short" if side == "long" else "long"
    opposite_side_since = _public_watchlist_cooldown_cutoff_minutes(
        attempted_at,
        PUBLIC_SIGNAL_OPPOSITE_SIDE_COOLDOWN_MINUTES,
    )
    if repository.has_recent_public_signal_for_symbol_direction(
        symbol=message.symbol,
        direction=opposite_side,
        since=opposite_side_since,
    ):
        return "public_block_same_symbol_opposite_side_cooldown"
    return None


def _public_watchlist_delivery_rate_block_reason(
    repository: SQLiteTelegramAlertAttemptRepository,
    message: TelegramSignalMessage,
    context: TelegramEligibilityContext,
    *,
    allow_public_watchlist: bool,
    daily_cap_reached: bool | None,
    hourly_cap_reached: bool | None,
    attempted_at: str,
) -> str | None:
    if daily_cap_reached is None:
        daily_cap_reached = _public_watchlist_daily_cap_reached(repository, context, attempted_at=attempted_at)
    if daily_cap_reached:
        return "public_block_daily_cap"
    if hourly_cap_reached is None:
        hourly_cap_reached = _public_watchlist_hourly_cap_reached(repository, context, attempted_at=attempted_at)
    if hourly_cap_reached:
        return "public_block_hourly_cap"
    if not allow_public_watchlist:
        return "public_block_scan_cap"
    cooldown_reason = _public_watchlist_symbol_cooldown_block_reason(
        repository,
        message,
        attempted_at=attempted_at,
    )
    if cooldown_reason is not None:
        return cooldown_reason
    return None

def _selected_public_watchlist_scan_signal_ids(
    symbol_results: Sequence[ScannerSymbolResult],
    context: TelegramEligibilityContext,
    *,
    repository: SQLiteTelegramAlertAttemptRepository,
    max_per_scan: int,
    cooldown_hours: int,
    attempted_at: str | None,
    daily_cap_reached: bool | None,
    hourly_cap_reached: bool | None,
) -> frozenset[str]:
    if max_per_scan <= 0:
        return frozenset()
    candidates_before_rate_limits: list[tuple[tuple[Any, ...], str, TelegramSignalMessage]] = []
    for symbol_result in symbol_results:
        signal_id = _public_watchlist_scan_signal_id(symbol_result, context)
        if signal_id == NA:
            continue
        message = _telegram_signal_message_for_alert(symbol_result, TelegramAlertType.WATCHLIST, context)
        prefilter = _public_watchlist_attempt_prefilter(symbol_result, message, context)
        if not prefilter.passed:
            continue
        gate = _public_watchlist_gate_result(symbol_result, message, context)
        if not gate.allowed:
            continue
        candidates_before_rate_limits.append((
            _public_watchlist_scan_candidate_sort_key(symbol_result, message),
            signal_id,
            message,
        ))
    if not candidates_before_rate_limits:
        return frozenset()
    checked_at = attempted_at or now_utc_iso()
    if daily_cap_reached is None:
        daily_cap_reached = _public_watchlist_daily_cap_reached(repository, context, attempted_at=checked_at)
    if daily_cap_reached:
        return frozenset()
    if hourly_cap_reached is None:
        hourly_cap_reached = _public_watchlist_hourly_cap_reached(repository, context, attempted_at=checked_at)
    if hourly_cap_reached:
        return frozenset()
    candidates: list[tuple[tuple[Any, ...], str]] = []
    for sort_key, signal_id, message in candidates_before_rate_limits:
        candidates.append((sort_key, signal_id))
    selected = [signal_id for _, signal_id in sorted(candidates, key=lambda item: item[0])[:max_per_scan]]
    return frozenset(selected)


def _public_watchlist_scan_signal_id(
    symbol_result: ScannerSymbolResult,
    context: TelegramEligibilityContext,
) -> str:
    transition = symbol_result.lifecycle_transition
    if transition is None or _alert_type_for_transition(symbol_result, transition) != TelegramAlertType.WATCHLIST:
        return NA
    message = _telegram_signal_message_for_alert(symbol_result, TelegramAlertType.WATCHLIST, context)
    return _public_watchlist_signal_id(symbol_result, message)


def _public_watchlist_scan_candidate_sort_key(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
) -> tuple[Any, ...]:
    score = _public_setup_quality_score_decimal(symbol_result) or Decimal("-1")
    rr = _decimal_or_none(message.planned_rr) or Decimal("-1")
    technical_score = _technical_score_decimal(symbol_result) or Decimal("-1")
    opportunity_score = _opportunity_score_decimal(symbol_result) or Decimal("-1")
    symbol = _symbol(message.symbol)
    return (
        -score,
        -rr,
        -technical_score,
        -opportunity_score,
        _major_liquidity_priority_index(symbol),
        symbol,
    )


def _major_liquidity_priority_index(symbol: str) -> int:
    try:
        return PUBLIC_SIGNAL_MAJOR_PRIORITY_SYMBOLS.index(symbol)
    except ValueError:
        return len(PUBLIC_SIGNAL_MAJOR_PRIORITY_SYMBOLS)

def _public_watchlist_cooldown_key(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
) -> str:
    return _public_watchlist_plan_id(symbol_result, message)


def _public_watchlist_cooldown_key_from_row(row: sqlite3.Row) -> str:
    plan_id = _row_value(row, "public_watchlist_plan_id")
    if _text(plan_id) != NA:
        return _text(plan_id)
    return _public_watchlist_cooldown_key_from_parts(
        symbol=row["symbol"],
        direction=row["direction"],
        entry_low=row["entry_low"],
        entry_high=row["entry_high"],
        stop_loss=row["stop_loss"],
        tp1=row["tp1"],
        tp2=row["tp2"],
        tp3=row["tp3"],
    )


def _public_watchlist_cooldown_key_from_parts(
    *,
    symbol: Any,
    direction: Any,
    entry_low: Any,
    entry_high: Any,
    stop_loss: Any,
    tp1: Any,
    tp2: Any,
    tp3: Any,
) -> str:
    normalized_symbol = _symbol(symbol)
    normalized_direction = _status_key(direction)
    if normalized_symbol == NA or normalized_direction not in {"long", "short"}:
        return NA
    low = _decimal_or_none(entry_low)
    high = _decimal_or_none(entry_high)
    invalidation = _decimal_or_none(stop_loss)
    if low is None or high is None or invalidation is None:
        return NA
    if low > high:
        low, high = high, low
    tick_size = _public_watchlist_tick_size_from_prices((low, high, invalidation))
    identity_parts = (
        PUBLIC_WATCHLIST_PLAN_ID_VERSION,
        normalized_symbol,
        normalized_direction,
        "liquidity_grab_pullback",
        _public_watchlist_normalized_price(low, tick_size),
        _public_watchlist_normalized_price(high, tick_size),
        _public_watchlist_quantized_price(
            invalidation,
            _public_watchlist_material_invalidation_quantum(low, high, tick_size),
        ),
    )
    plan_hash = hashlib.sha256("|".join(_text(part) for part in identity_parts).encode("utf-8")).hexdigest()[:20]
    return f"{normalized_symbol}|{normalized_direction}|{plan_hash}"


def _parse_iso_datetime(value: Any) -> datetime:
    text = _text(value)
    if text == NA:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _integer_or_zero(value: Any) -> int:
    number = _decimal_or_none(value)
    if number is None:
        return 0
    return int(number)


def _watch_zone_text(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    return _first_non_na(
        diagnostics.get("watch_zone"),
        diagnostics.get("entry_zone"),
        diagnostics.get("pullback_zone"),
        diagnostics.get("preferred_fib_pullback_zone"),
        diagnostics.get("fib_pullback_zone"),
    )


def _watchlist_context_sentence(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    failed_gate = _watch_failed_gate(symbol_result, diagnostics)
    reason = _watch_reason(symbol_result, diagnostics)
    structure = _watch_structure_context(symbol_result, diagnostics)
    planned_rr = _decimal_or_none(_first_non_na(diagnostics.get("rr_to_tp2"), diagnostics.get("planned_rr")))
    rr_missing = planned_rr is None
    rr_below_default = planned_rr is not None and planned_rr < DEFAULT_CONFIRMED_MIN_RR

    if failed_gate in WATCHLIST_STALE_OR_INCOMPLETE_GATES:
        sentence = (
            "The earlier structure is no longer clean enough for confirmation. "
            "A fresh liquidity sweep and new BOS/CHoCH are required before this candidate can become valid."
        )
    elif failed_gate in WATCHLIST_CONFIRMATION_GATES:
        sentence = (
            "Price has swept liquidity, but a fresh LTF BOS/CHoCH confirmation is still required "
            "before this candidate can become valid."
        )
    elif failed_gate in WATCHLIST_OB_FVG_GATES:
        sentence = (
            f"{structure}, but the setup is still waiting for a valid OB/FVG pullback zone "
            "before it can become a confirmed signal."
        )
    elif "fib" in failed_gate:
        sentence = f"{structure}, but fib alignment still needs confirmation before the setup can activate."
    elif failed_gate in WATCHLIST_RR_GATES or rr_below_default:
        sentence = f"{structure}, but final RR still needs validation before the setup can activate."
    elif _watchlist_has_public_plan_levels(diagnostics):
        sentence = (
            "Price has produced a trackable pullback map, but the setup remains watchlist-only until "
            "final RR, structure, and quality gates confirm."
        )
    elif reason != NA:
        sentence = f"{structure}, but the setup is not confirmed yet. {_clean_watch_text(reason)}"
    else:
        sentence = f"{structure}, but the setup is not confirmed yet."

    if not sentence.endswith((".", "!", "?")):
        sentence = f"{sentence}."
    if rr_missing and "RR still needs validation" not in sentence:
        sentence = f"{sentence} Final RR still needs validation after the entry zone forms."
    elif rr_below_default and "RR" not in sentence:
        sentence = f"{sentence} Final RR must improve to the configured minimum before confirmation."
    return sentence


def _watchlist_needs_next(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> tuple[str, ...]:
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    failed_gate = _watch_failed_gate(symbol_result, diagnostics)
    direction = _first_non_na(
        diagnostics.get("bias"),
        diagnostics.get("direction"),
        getattr(symbol_result.lifecycle_state, "direction", NA) if symbol_result.lifecycle_state is not None else NA,
    )
    if failed_gate in WATCHLIST_STALE_OR_INCOMPLETE_GATES:
        return (
            "A fresh BOS/CHoCH is needed if the current structure becomes stale.",
            "Pullback must remain inside the planned zone.",
            _directional_invalidation_condition(direction),
        )
    if failed_gate in WATCHLIST_CONFIRMATION_GATES:
        return (
            "Price must trade into the Limit Zone.",
            "Structure must continue to respect the sweep/BOS/CHoCH context.",
            _directional_invalidation_condition(direction),
        )
    if failed_gate in WATCHLIST_OB_FVG_GATES:
        return (
            "A valid OB/FVG zone must form inside the displacement impulse.",
            "Price must react from the planned Limit Zone.",
            _directional_invalidation_condition(direction),
        )

    candidates = (
        _field(intelligence, "next_required_conditions"),
        diagnostics.get("needs_next"),
        diagnostics.get("next_required_conditions"),
        diagnostics.get("next_conditions"),
        diagnostics.get("next_condition"),
        diagnostics.get("next_trigger_needed"),
    )
    lines: list[str] = []
    for candidate in candidates:
        for value in _sequence_or_single(candidate):
            text = _clean_watch_text(value)
            if text != NA and _chart_only_watch_condition(text) and text not in lines:
                lines.append(text)
            if len(lines) == 3:
                return tuple(lines)
    if lines:
        while len(lines) < 3:
            fallback = _fallback_watchlist_needs_next(direction)[len(lines)]
            if fallback not in lines:
                lines.append(fallback)
            else:
                break
        return tuple(lines[:3])
    return _fallback_watchlist_needs_next(direction)


def _fallback_watchlist_needs_next(direction: Any) -> tuple[str, str, str]:
    side = _status_key(direction)
    if side == "long":
        return (
            "Price must trade into the Limit Zone.",
            "Limit Zone must hold as support after the pullback.",
            "Bullish structure must remain valid above the invalidation level.",
        )
    if side == "short":
        return (
            "Price must trade into the Limit Zone.",
            "Limit Zone must hold as resistance after the pullback.",
            "Bearish structure must remain valid below the invalidation level.",
        )
    return (
        "Price must interact with the Limit Zone.",
        "Structure must remain valid.",
        "Invalidation level must hold.",
    )


def _directional_invalidation_condition(direction: Any) -> str:
    side = _status_key(direction)
    if side == "long":
        return "Price must not accept beyond the invalidation level."
    if side == "short":
        return "Price must not accept beyond the invalidation level."
    return "Invalidation level must hold."


def _chart_only_watch_condition(value: Any) -> bool:
    text = _text(value).lower()
    if text == NA.lower():
        return False
    tokens = text.replace("/", " ").replace("-", " ").replace(".", " ").replace(",", " ").split()
    forbidden = (
        "trust meter",
        "risk/reward",
        "risk reward",
        "score",
        "scoring",
        "opportunity score",
        "quality score",
        "final confluence threshold",
        "scanner threshold",
        "grade",
        "hard rejection",
        "required threshold",
        "quality gate",
        "final quality",
        "core engine",
    )
    return "rr" not in tokens and not any(fragment in text for fragment in forbidden)


def _watchlist_has_public_plan_levels(diagnostics: Mapping[str, Any]) -> bool:
    return (
        _numeric_pair_values(diagnostics.get("entry_low"), diagnostics.get("entry_high"))
        or _decimal_or_none(diagnostics.get("stop")) is not None
        or _decimal_or_none(diagnostics.get("stop_loss")) is not None
    )


def _append_rr_requirement(lines: Sequence[str], diagnostics: Mapping[str, Any]) -> tuple[str, ...]:
    output = [line for line in lines if _text(line) != NA]
    planned_rr = _decimal_or_none(_first_non_na(diagnostics.get("rr_to_tp2"), diagnostics.get("planned_rr")))
    if planned_rr is None or planned_rr >= DEFAULT_CONFIRMED_MIN_RR:
        return tuple(output[:3])
    requirement = "Final RR must improve to at least the configured minimum before confirmation."
    if any("rr" in line.lower() for line in output):
        return tuple(output[:3])
    if len(output) >= 3:
        output[2] = requirement
    else:
        output.append(requirement)
    return tuple(output[:3])


def _watchlist_invalidation_sentence(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    *,
    direction: Any,
    stop_loss: Any,
) -> str:
    stop = format_telegram_price(stop_loss)
    side = _status_key(direction)
    if stop != NA:
        side_word = "below" if side == "long" else "above" if side == "short" else "beyond"
        return f"Invalid if price accepts {side_word} {stop}."

    raw = _clean_public_sentence(
        _first_non_na(
            _near_miss_invalidation_hint(symbol_result, diagnostics),
            diagnostics.get("watchlist_invalidation"),
            diagnostics.get("invalidation_hint"),
            getattr(symbol_result.lifecycle_state, "invalidation_reason", NA),
        )
    )
    if raw != NA:
        return _as_watchlist_invalidation(raw)
    return NA


def _as_watchlist_invalidation(value: Any) -> str:
    text = _clean_watch_text(value)
    if text == NA:
        return NA
    key = text.lower()
    if key.startswith("watchlist invalidates"):
        return text
    if key.startswith("invalidated if"):
        return "Watchlist invalidates if" + text[len("Invalidated if") :]
    if key.startswith("invalidates if"):
        return "Watchlist invalidates if" + text[len("Invalidates if") :]
    if key.startswith("invalid if"):
        return "Watchlist invalidates if" + text[len("Invalid if") :]
    if key.startswith("signal invalidates if"):
        return "Watchlist invalidates if" + text[len("Signal invalidates if") :]
    return text


def _watch_structure_context(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    gates_passed = {_status_key(value) for value in _sequence_or_single(diagnostics.get("gates_passed"))}
    sweep = (
        bool(symbol_result.sweep_detected)
        or _status_key(diagnostics.get("execution_sweep_status")) == "passed"
        or "sweep" in gates_passed
    )
    confirmation = (
        bool(symbol_result.bos_detected or symbol_result.choch_detected)
        or _status_key(diagnostics.get("confirmation_structure_shift_status")) == "passed"
        or "bos_choch" in gates_passed
    )
    if sweep and confirmation:
        return "Price has swept liquidity and confirmed a LTF BOS/CHoCH"
    if sweep:
        return "Price has produced a sweep, while LTF BOS/CHoCH still needs confirmation"
    if confirmation:
        return "LTF structure has shifted, while the sweep context still needs confirmation"
    return "Core structure is still developing"


def _watch_failed_gate(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    return _status_key(
        _first_non_na(
            _field(intelligence, "primary_failed_gate"),
            diagnostics.get("first_failed_gate"),
            getattr(symbol_result.lifecycle_state, "failed_gate", NA),
            symbol_result.rejection_stage,
        )
    )


def _watch_reason(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    return _first_non_na(
        _field(intelligence, "short_reason"),
        diagnostics.get("pullback_failure_reason"),
        diagnostics.get("ob_fvg_diagnostics"),
        diagnostics.get("fib_diagnostics"),
        diagnostics.get("rr_diagnostics"),
        symbol_result.rejection_reason,
    )


def _near_miss_invalidation_hint(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    return _first_non_na(_field(intelligence, "invalidation_hint"), diagnostics.get("invalidation_hint"))


def _near_miss_intelligence(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Any:
    intelligence = symbol_result.near_miss_intelligence
    if intelligence is not None:
        return intelligence
    payload = diagnostics.get("near_miss_intelligence")
    return payload if isinstance(payload, Mapping) else None


def _alert_type_for_transition(
    symbol_result: ScannerSymbolResult,
    transition: SetupTransitionResult,
) -> TelegramAlertType | None:
    state = transition.to_state
    if state in WATCH_ALERT_STATES:
        return TelegramAlertType.WATCHLIST
    if state in SIGNAL_ALERT_STATES:
        return TelegramAlertType.SIGNAL_CONFIRMED
    if state == SetupLifecycleState.MANAGING:
        return TelegramAlertType.LIMIT_HIT
    if state == SetupLifecycleState.TP_HIT:
        return _tp_alert_type(symbol_result)
    if state == SetupLifecycleState.SL_HIT:
        return TelegramAlertType.SL_HIT
    if state == SetupLifecycleState.INVALIDATED:
        return TelegramAlertType.INVALIDATED
    if state == SetupLifecycleState.EXPIRED:
        return TelegramAlertType.EXPIRED
    if state == SetupLifecycleState.COOLDOWN:
        return TelegramAlertType.NO_LONGER_TRACKING
    if _explicit_watchlist_candidate(symbol_result):
        return TelegramAlertType.WATCHLIST
    return None


def _tp_alert_type(symbol_result: ScannerSymbolResult) -> TelegramAlertType | None:
    diagnostics = _representative_diagnostics(symbol_result)
    for value in (
        diagnostics.get("telegram_alert_type"),
        diagnostics.get("outcome_status"),
        diagnostics.get("tp_hit"),
        diagnostics.get("highest_tp_hit"),
    ):
        text = _status_key(value)
        if text in {"tp3", "tp3_hit", "3"}:
            return TelegramAlertType.TP3_HIT
        if text in {"tp2", "tp2_hit", "2"}:
            return TelegramAlertType.TP2_HIT
        if text in {"tp1", "tp1_hit", "1"}:
            return TelegramAlertType.TP1_HIT
    return None


def _requires_prior_active_alert(alert_type: TelegramAlertType) -> bool:
    return alert_type not in {TelegramAlertType.WATCHLIST, TelegramAlertType.SIGNAL_CONFIRMED}


def _telegram_message_type_for_alert(
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage | None = None,
) -> TelegramMessageType:
    if alert_type == TelegramAlertType.RESEARCH_WATCH:
        return TelegramMessageType.RESEARCH_WATCH
    if alert_type == TelegramAlertType.SIGNAL_CONFIRMED and message is not None and message.upgraded_from_watchlist:
        return TelegramMessageType.WATCHLIST_CONFIRMED
    if alert_type == TelegramAlertType.WATCHLIST:
        return TelegramMessageType.PUBLIC_WATCHLIST
    if alert_type == TelegramAlertType.SIGNAL_CONFIRMED:
        return TelegramMessageType.PUBLIC_SIGNAL
    if alert_type == TelegramAlertType.LIMIT_HIT:
        return TelegramMessageType.LIMIT_ZONE_HIT
    if alert_type in {TelegramAlertType.TP1_HIT, TelegramAlertType.TP2_HIT, TelegramAlertType.TP3_HIT}:
        return TelegramMessageType.TP_HIT
    if alert_type == TelegramAlertType.SL_HIT:
        return TelegramMessageType.SL_HIT
    if alert_type in TERMINAL_UPDATE_ALERT_TYPES:
        return TelegramMessageType.INVALIDATED
    return TelegramMessageType.LIFECYCLE_UPDATE


def _terminal_transition_blockers(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
) -> tuple[str, ...]:
    transition = symbol_result.lifecycle_transition
    if transition is None:
        return ("missing_lifecycle_transition",)
    previous = _previous_transition_state(symbol_result)
    if previous is None:
        return ("terminal_transition_missing_previous_state",)

    if alert_type == TelegramAlertType.INVALIDATED:
        allowed = WATCH_ALERT_STATES | SIGNAL_ALERT_STATES | {SetupLifecycleState.EXECUTING, SetupLifecycleState.MANAGING}
    elif alert_type == TelegramAlertType.EXPIRED:
        allowed = WATCH_ALERT_STATES | SIGNAL_ALERT_STATES | {SetupLifecycleState.EXECUTING, SetupLifecycleState.MANAGING}
    elif alert_type == TelegramAlertType.NO_LONGER_TRACKING:
        allowed = WATCH_ALERT_STATES | {SetupLifecycleState.INVALIDATED, SetupLifecycleState.EXPIRED}
    else:
        allowed = set()

    if previous not in allowed:
        return (f"terminal_transition_not_meaningful:{previous.value}->{transition.to_state.value}",)
    return ()


def _previous_transition_state(symbol_result: ScannerSymbolResult) -> SetupLifecycleState | None:
    transition = symbol_result.lifecycle_transition
    if transition is not None and transition.from_state is not None:
        return transition.from_state
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is not None:
        return lifecycle.previous_state
    return None


def _confirmed_rejection_context(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage | None = None,
) -> ConfirmedSignalRejectionContext:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    active_reasons: list[str] = []
    historical_reasons: list[str] = []

    active_reasons.extend(_diagnostic_texts(diagnostics, ACTIVE_REJECTION_REASON_KEYS))
    active_reasons.extend(_diagnostic_sequence_texts(diagnostics, ACTIVE_REJECTION_REASONS_KEYS))
    historical_reasons.extend(_diagnostic_texts(diagnostics, HISTORICAL_REJECTION_REASON_KEYS))
    historical_reasons.extend(_diagnostic_sequence_texts(diagnostics, HISTORICAL_REJECTION_REASONS_KEYS))

    direct_reasons = [
        _text(symbol_result.rejection_reason),
        *(_text(reason) for reason in symbol_result.rejection_reasons),
    ]
    direct_reasons = [reason for reason in direct_reasons if reason != NA]
    if _confirmed_currently_rejected(symbol_result):
        active_reasons.extend(direct_reasons)
    else:
        historical_reasons.extend(direct_reasons)

    explicit_failed_gate = _first_non_na(*_diagnostic_texts(diagnostics, ACTIVE_FAILED_GATE_KEYS))
    lifecycle_failed_gate = getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA
    active_failed_gate = _first_non_na(explicit_failed_gate)
    historical_failed_gate = NA
    if _text(active_failed_gate) == NA and _confirmed_lifecycle_failed_gate_active(symbol_result, lifecycle_failed_gate):
        active_failed_gate = _text(lifecycle_failed_gate)
    elif _text(lifecycle_failed_gate) != NA:
        historical_failed_gate = _text(lifecycle_failed_gate)

    active_invalidation = _confirmed_active_invalidation_reason(symbol_result, message, active_failed_gate=active_failed_gate)
    historical_invalidation = NA
    lifecycle_invalidation = getattr(lifecycle, "invalidation_reason", NA) if lifecycle is not None else NA
    if _text(active_invalidation) == NA and _text(lifecycle_invalidation) != NA:
        historical_invalidation = _text(lifecycle_invalidation)

    return ConfirmedSignalRejectionContext(
        active_rejection_reasons=tuple(dict.fromkeys(active_reasons)),
        historical_rejection_reasons=tuple(dict.fromkeys(historical_reasons)),
        active_failed_gate=_text(active_failed_gate),
        historical_failed_gate=_text(historical_failed_gate),
        active_invalidation_reason=_text(active_invalidation),
        historical_invalidation_reason=_text(historical_invalidation),
    )


def _confirmed_currently_rejected(symbol_result: ScannerSymbolResult) -> bool:
    current_status = _status_key(getattr(symbol_result.status, "value", symbol_result.status))
    if current_status in CONFIRMED_REJECTED_STATUS_KEYS:
        return True
    return _status_key(_lifecycle_state_text(symbol_result)) in CONFIRMED_TERMINAL_OR_REJECTED_STATES


def _confirmed_lifecycle_failed_gate_active(symbol_result: ScannerSymbolResult, failed_gate: Any) -> bool:
    gate_key = _status_key(failed_gate)
    if not gate_key:
        return False
    if _confirmed_currently_rejected(symbol_result):
        return True

    diagnostics = _representative_diagnostics(symbol_result)
    if _diagnostic_texts(diagnostics, ACTIVE_FAILED_GATE_KEYS + ACTIVE_REJECTION_REASON_KEYS):
        return True
    if _diagnostic_sequence_texts(diagnostics, ACTIVE_REJECTION_REASONS_KEYS):
        return True

    lifecycle = symbol_result.lifecycle_state
    action_label = _status_key(getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA)
    invalidation = _text(getattr(lifecycle, "invalidation_reason", NA) if lifecycle is not None else NA)
    invalidation_key = _status_key(invalidation)
    if any(token in action_label for token in ("reject", "no_trade", "removed", "cooldown")):
        return True
    if "wait" in action_label and (
        gate_key in CONFIRMED_ACTIVE_FAILED_GATE_KEYS
        or _looks_like_rejection_reason(invalidation)
        or "regime" in gate_key
    ):
        return True
    if _looks_like_rejection_reason(invalidation) and (
        gate_key in CONFIRMED_ACTIVE_FAILED_GATE_KEYS or any(token in gate_key for token in ("regime", "score", "rr"))
    ):
        return True
    if gate_key in {
        "structural_breakdown",
        "body_acceptance_failure",
        "pullback_too_deep",
        "pullback_beyond_786",
    } and any(token in invalidation_key for token in ("broke", "broken", "failed", "invalidated", "accepted_beyond")):
        return True
    return False


def _confirmed_active_invalidation_reason(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage | None = None,
    *,
    active_failed_gate: Any = NA,
) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    lifecycle = symbol_result.lifecycle_state

    explicit = _first_non_na(*_diagnostic_texts(diagnostics, ACTIVE_INVALIDATION_REASON_KEYS))
    if _text(explicit) != NA:
        return _text(explicit)

    for value in (
        getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA,
        _field(setup, "invalidation"),
    ):
        if _looks_like_rejection_reason(value):
            return _text(value)

    lifecycle_invalidation = getattr(lifecycle, "invalidation_reason", NA) if lifecycle is not None else NA
    if _text(active_failed_gate) != NA and _text(lifecycle_invalidation) != NA:
        return _text(lifecycle_invalidation)

    if _confirmed_currently_rejected(symbol_result):
        diagnostic_invalidation = _first_non_na(diagnostics.get("invalidation"), diagnostics.get("invalidation_reason"))
        if _looks_like_rejection_reason(diagnostic_invalidation):
            return _text(diagnostic_invalidation)

    if message is not None and _looks_like_rejection_reason(message.invalidation_reason):
        message_text = _text(message.invalidation_reason)
        source_values = (
            getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA,
            _field(setup, "invalidation"),
            *_diagnostic_texts(diagnostics, ACTIVE_INVALIDATION_REASON_KEYS),
        )
        if any(_text(value) == message_text for value in source_values):
            return message_text
    return NA


def _diagnostic_texts(diagnostics: Mapping[str, Any], keys: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        text = _text(diagnostics.get(key))
        if text != NA:
            values.append(text)
    return tuple(values)


def _diagnostic_sequence_texts(diagnostics: Mapping[str, Any], keys: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        for value in _sequence_or_single(diagnostics.get(key)):
            text = _text(value)
            if text != NA:
                values.append(text)
    return tuple(values)


def _failed_confirmation_terminal_decision(
    symbol_result: ScannerSymbolResult,
    *,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
    blockers: Sequence[str],
    lifecycle_transition: SetupTransitionResult | None,
    prior_public_alert: TelegramAlertAttemptRecord | None,
    terminal_identity_failure_reason: str | None,
) -> TelegramAlertDecision | None:
    if alert_type != TelegramAlertType.SIGNAL_CONFIRMED:
        return None
    if not _failed_confirmation_evidence(symbol_result, blockers):
        return None

    terminal_alert_type = (
        TelegramAlertType.INVALIDATED
        if _failed_confirmation_is_structural_invalidation(symbol_result, blockers)
        else TelegramAlertType.NO_LONGER_TRACKING
    )
    failed_message = replace(
        message,
        invalidation_reason=_failed_confirmation_reason(symbol_result, blockers, terminal_alert_type),
    )
    if prior_public_alert is not None and prior_public_alert.alert_type == TelegramAlertType.WATCHLIST.value:
        return TelegramAlertDecision(
            True,
            "eligible_failed_confirmation_update",
            alert_type=terminal_alert_type,
            message=replace(
                _message_with_prior_public_identity(failed_message, prior_public_alert),
                was_watchlist=prior_public_alert.alert_type == TelegramAlertType.WATCHLIST.value,
            ),
            lifecycle_transition=lifecycle_transition,
        )

    if terminal_identity_failure_reason in {
        "terminal_update_identity_ambiguous",
        "terminal_update_identity_not_matched",
    }:
        return TelegramAlertDecision(
            False,
            terminal_identity_failure_reason,
            alert_type=terminal_alert_type,
            message=failed_message,
            lifecycle_transition=lifecycle_transition,
        )
    return None


def _failed_confirmation_core_blockers(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    blockers: list[str] = []
    context = _confirmed_rejection_context(symbol_result)
    failed_gate = _status_key(context.active_failed_gate)
    if failed_gate:
        blockers.append(f"failed_confirmation_gate:{failed_gate}")

    text = _failed_confirmation_haystack(symbol_result, active_only=True)
    text_key = _status_key(text)
    if "rejected_by_regime" in text_key or "regime_compatibility" in text_key or "regime_weakness" in text_key:
        blockers.append("failed_confirmation_text:regime_compatibility")
    if "technical_score_below" in text_key or "low_technical" in text_key:
        blockers.append("failed_confirmation_text:technical_quality")
    if "opportunity_score_below" in text_key or "score_below" in text_key:
        blockers.append("failed_confirmation_text:final_score")
    if "rr_below" in text_key or "risk_reward_below" in text_key or "below_minimum" in text_key:
        blockers.append("failed_confirmation_text:final_rr")
    if "quality_gate_failed" in text_key:
        blockers.append("failed_confirmation_text:quality_gate")
    if "setup_rejected" in text_key or "not_public_ready" in text_key:
        blockers.append("failed_confirmation_text:setup_rejected")

    lifecycle = symbol_result.lifecycle_state
    action_label = _status_key(getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA)
    if failed_gate and action_label and any(token in action_label for token in ("reject", "wait", "no_trade", "removed")):
        blockers.append(f"failed_confirmation_action:{action_label}")
    return tuple(dict.fromkeys(blockers))


def _failed_confirmation_evidence(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
) -> bool:
    if any(str(blocker).startswith("failed_confirmation_") for blocker in blockers):
        return True
    if _text(_confirmed_rejection_context(symbol_result).active_failed_gate) != NA:
        return True
    return bool(blockers)


def _failed_confirmation_is_structural_invalidation(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
) -> bool:
    haystack = " ".join(
        (
            _failed_confirmation_haystack(symbol_result, active_only=True),
            " ".join(str(blocker) for blocker in blockers),
        )
    )
    key = _status_key(haystack)
    return any(
        token in key
        for token in (
            "structural_breakdown",
            "body_acceptance_failure",
            "pullback_too_deep",
            "pullback_beyond_786",
            "structure_failed",
            "structure_broke",
        )
    )


def _failed_confirmation_reason(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
    alert_type: TelegramAlertType,
) -> str:
    if alert_type == TelegramAlertType.INVALIDATED:
        return "Watchlist invalidated because the sweep/BOS/CHoCH structure failed before confirmation."

    haystack = " ".join(
        (_failed_confirmation_haystack(symbol_result, active_only=True), " ".join(str(blocker) for blocker in blockers))
    )
    key = _status_key(haystack)
    if "regime_compatibility" in key or "regime_weakness" in key or "rejected_by_regime" in key:
        return "Watchlist removed because market conditions failed before confirmation."
    if "rr_below" in key or "low_rr" in key or "risk_reward_below" in key:
        return "Watchlist removed because final RR or target quality failed before confirmation."
    if "target_integrity" in key:
        return "Watchlist removed because final RR or target quality failed before confirmation."
    if "technical" in key:
        return "Watchlist removed because technical quality failed before confirmation."
    if "opportunity" in key or "scoring" in key or "score_below" in key:
        return "Watchlist removed because final score requirements were not met before confirmation."
    if "quality_gate" in key or "setup_quality" in key:
        return "Watchlist removed because the setup failed final confirmation gates."
    if "rejected" in key or "not_public_ready" in key:
        return "Watchlist removed because the setup failed final confirmation gates."
    return "Watchlist removed because the setup failed final confirmation gates."


def _failed_confirmation_haystack(symbol_result: ScannerSymbolResult, *, active_only: bool = False) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    transition = symbol_result.lifecycle_transition
    if active_only:
        context = _confirmed_rejection_context(symbol_result)
        action_label = getattr(lifecycle, "action_label", NA) if _text(context.active_failed_gate) != NA else NA
        parts = [
            context.active_failed_gate,
            context.active_invalidation_reason,
            action_label,
            getattr(transition, "notes", NA) if _confirmed_currently_rejected(symbol_result) else NA,
            *context.active_rejection_reasons,
        ]
        return " ".join(_text(part) for part in parts if _text(part) != NA)
    parts = [
        getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
        getattr(lifecycle, "invalidation_reason", NA) if lifecycle is not None else NA,
        getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA,
        getattr(transition, "notes", NA) if transition is not None else NA,
        symbol_result.rejection_reason,
        *symbol_result.rejection_reasons,
        diagnostics.get("reason"),
        diagnostics.get("invalidation_reason"),
        diagnostics.get("regime_reason"),
        diagnostics.get("regime_diagnostics"),
        diagnostics.get("first_failed_gate"),
        diagnostics.get("failed_gate"),
    ]
    return " ".join(_text(part) for part in parts if _text(part) != NA)


def _terminal_update_reason(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
) -> str:
    raw_reason = _terminal_raw_reason(symbol_result, alert_type)
    cleaned = _clean_terminal_reason(raw_reason)
    if alert_type == TelegramAlertType.INVALIDATED:
        if cleaned != NA:
            return _terminal_sentence("Watchlist invalidated because", cleaned)
        return "Watchlist is no longer valid according to the lifecycle engine."
    if alert_type == TelegramAlertType.EXPIRED:
        if cleaned != NA and _status_key(cleaned) != "setup_expired_before_completion":
            return _terminal_sentence("Watchlist expired because", cleaned)
        return "Watchlist expired because it did not confirm within the valid tracking window."
    if alert_type == TelegramAlertType.NO_LONGER_TRACKING:
        if cleaned != NA and "cooldown" not in cleaned.lower():
            return _terminal_sentence("Watchlist removed because", cleaned)
        return "Watchlist removed because the setup entered cooldown before confirmation."
    return "Watchlist is no longer valid according to the lifecycle engine."


def _terminal_raw_reason(symbol_result: ScannerSymbolResult, alert_type: TelegramAlertType) -> Any:
    diagnostics = _representative_diagnostics(symbol_result)
    transition = symbol_result.lifecycle_transition
    lifecycle = symbol_result.lifecycle_state
    if alert_type == TelegramAlertType.EXPIRED:
        return _first_non_na(
            diagnostics.get("terminal_reason"),
            diagnostics.get("expired_reason"),
            getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
            diagnostics.get("first_failed_gate"),
            diagnostics.get("failed_gate"),
        )
    if alert_type == TelegramAlertType.NO_LONGER_TRACKING:
        return _first_non_na(
            diagnostics.get("terminal_reason"),
            diagnostics.get("cooldown_reason"),
            getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA,
            getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
            diagnostics.get("first_failed_gate"),
            diagnostics.get("failed_gate"),
        )
    return _first_non_na(
        getattr(lifecycle, "invalidation_reason", NA) if lifecycle is not None else NA,
        getattr(transition, "notes", NA) if transition is not None else NA,
        getattr(getattr(transition, "reason", None), "value", NA) if transition is not None else NA,
        getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
        getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA,
        diagnostics.get("terminal_reason"),
        diagnostics.get("invalidation_reason"),
        diagnostics.get("pullback_failure_reason"),
        diagnostics.get("first_failed_gate"),
        diagnostics.get("failed_gate"),
    )


def _clean_terminal_reason(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return NA
    key = _status_key(text.rstrip(".!?"))
    mapped = {
        "setup_invalidated_by_current_structure_or_failed_gate": "the sweep/BOS/CHoCH structure failed before confirmation",
        "setup_expired_before_completion": "it did not confirm within the valid tracking window",
        "lifecycle_moved_into_cooldown": "the setup entered cooldown before confirmation",
        "entry_window_expired": "it did not confirm within the valid tracking window",
        "pullback_too_deep": "price accepted beyond the planned invalidation structure",
        "pullback_beyond_786": "price accepted beyond the planned invalidation structure",
        "body_acceptance_failure": "price accepted beyond the planned invalidation structure",
        "structural_breakdown": "the sweep/BOS/CHoCH structure failed before confirmation",
        "no_displacement_candle": "the required displacement structure failed before confirmation",
        "missing_displacement_impulse": "the required displacement structure failed before confirmation",
    }.get(key)
    if mapped is not None:
        return mapped
    lowered = text.lower()
    if (
        _looks_like_rejection_reason(text)
        or "decimal(" in lowered
        or "{" in text
        or "}" in text
        or lowered in {"true", "false"}
    ):
        return NA
    if key.startswith("invalid_if") or key.startswith("invalidates_if") or key.startswith("signal_invalidates_if"):
        return "price accepted beyond the planned invalidation structure"
    return _plain_label(text) if "_" in text and len(text.split()) == 1 else text


def _terminal_sentence(prefix: str, reason: str) -> str:
    cleaned = _clean_watch_text(reason)
    if cleaned == NA:
        return "Watchlist is no longer valid according to the lifecycle engine."
    lower = cleaned.lower()
    if lower.startswith(("watchlist ", "signal ", "setup ")):
        return cleaned
    return f"{prefix} {cleaned[:1].lower()}{cleaned[1:]}"


def _public_signal_gate_result(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
    *,
    prior_public_alert: TelegramAlertAttemptRecord | None = None,
) -> PublicSignalGateResult:
    if alert_type not in {TelegramAlertType.SIGNAL_CONFIRMED, TelegramAlertType.LIMIT_HIT}:
        return PublicSignalGateResult(True)

    state = _public_gate_state(symbol_result)
    state_key = _status_key(state)
    setup_id = _signal_id(symbol_result)
    symbol = _symbol(message.symbol)
    reasons: list[str] = []

    if alert_type == TelegramAlertType.SIGNAL_CONFIRMED:
        if not is_public_signal_eligible_state(state_key):
            reasons.append(f"public_signal_state_not_confirmed:{state_key or 'missing'}")
        if is_internal_touch_state(state_key):
            reasons.append(f"internal_touch_state_not_public_signal:{state_key}")
    elif alert_type == TelegramAlertType.LIMIT_HIT:
        if prior_public_alert is None:
            reasons.append("limit_hit_requires_prior_public_signal")
        if not is_public_active_state(state_key):
            reasons.append(f"limit_hit_state_not_public_active:{state_key or 'missing'}")
        if is_internal_touch_state(state_key):
            reasons.append(f"limit_hit_internal_touch_state:{state_key}")
        if prior_public_alert is not None and setup_id != NA and prior_public_alert.signal_id != setup_id:
            reasons.append("limit_hit_signal_id_mismatch")

    missing = _missing_required_fields(alert_type, message)
    if missing:
        reasons.append(f"missing_required_fields:{','.join(missing)}")
        if alert_type == TelegramAlertType.SIGNAL_CONFIRMED:
            if "direction" in missing:
                reasons.append("confirmed_missing_side")
            if "entry_low" in missing or "entry_high" in missing:
                reasons.append("confirmed_missing_entry_zone")
            if "stop_loss" in missing:
                reasons.append("confirmed_missing_stop")
            if "invalidation_reason" in missing:
                reasons.append("confirmed_missing_invalidation")

    if alert_type != TelegramAlertType.SIGNAL_CONFIRMED:
        if _text(symbol_result.rejection_reason) != NA:
            reasons.append("rejection_reason_present")
        if any(_text(reason) != NA for reason in symbol_result.rejection_reasons):
            reasons.append("rejection_reasons_present")

    if _status_key(_first_non_na(_lifecycle_state_text(symbol_result), state)) in {
        "invalidated",
        "expired",
        "cooldown",
        "rejected",
        "reject",
    }:
        reasons.append(f"terminal_or_rejected_state:{state_key or 'missing'}")

    return PublicSignalGateResult(
        allowed=not reasons,
        reason_codes=tuple(_status_key(reason) for reason in reasons),
        blocking_reasons=tuple(dict.fromkeys(reasons)),
        state=state_key or NA,
        setup_id=None if setup_id == NA else setup_id,
        symbol=None if symbol == NA else symbol,
    )


def _public_watchlist_gate_result(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    context: TelegramEligibilityContext,
) -> PublicWatchlistGateResult:
    candidate = _public_watchlist_candidate_from_symbol(symbol_result)
    state = _public_gate_state(symbol_result)
    state_key = _status_key(state)
    setup_id = _signal_id(symbol_result)
    symbol = _symbol(message.symbol)
    failed_gate_codes = _public_watchlist_failed_gate_codes(symbol_result, candidate=candidate)
    failed_gate_classes = _public_watchlist_failed_gate_classes(failed_gate_codes)
    failed_gate_class_set = frozenset(failed_gate_classes)
    allowed_missing_gate = _public_watchlist_allowed_missing_gate(failed_gate_classes)
    rejected_state_allowed = _public_watchlist_rejected_state_allowed(
        symbol_result,
        candidate,
        failed_gate_codes,
        failed_gate_classes,
    )
    planned_rr = _decimal_or_none(candidate.potential_rr)
    target_caution = _public_watchlist_target_caution_actionable(symbol_result)
    min_rr = (
        PUBLIC_SIGNAL_TARGET_CAUTION_MIN_RR
        if target_caution
        else max(_decimal_or_default(context.public_watchlist_min_rr, PUBLIC_WATCHLIST_MIN_RR), PUBLIC_WATCHLIST_MIN_RR)
    )
    reasons: list[str] = []
    reasons.extend(_public_watchlist_v1_blockers(symbol_result, message, context, candidate=candidate))

    if not context.public_watchlist_enabled:
        reasons.append("public_watchlist_disabled")
    if not _explicit_public_watchlist_source(symbol_result, candidate):
        reasons.append("public_watchlist_missing_explicit_source")
    if not _public_watchlist_state_allowed(symbol_result, candidate, failed_gate_codes, failed_gate_classes):
        reasons.append(f"public_watchlist_state_not_eligible:{state_key or 'missing'}")
    if state_key in PUBLIC_WATCHLIST_BLOCKED_STATE_KEYS:
        reasons.append(f"public_watchlist_terminal_state:{state_key}")
    if state_key in PUBLIC_WATCHLIST_REJECTED_STATE_KEYS and not rejected_state_allowed:
        reasons.append(f"public_watchlist_rejected_state_not_watchlist_eligible:{state_key}")

    expiry = _watchlist_candidate_expiry_decision(symbol_result)
    if expiry.expired:
        reasons.append("public_watchlist_stale_or_expired")

    reasons.extend(_public_watchlist_quality_gate_blockers(candidate, min_grade=context.public_watchlist_min_grade))
    if target_caution:
        grade_blocker = _public_watchlist_target_caution_grade_blocker(candidate)
        if grade_blocker != NA:
            reasons.append(grade_blocker)
        if planned_rr is None or planned_rr < PUBLIC_SIGNAL_TARGET_CAUTION_MIN_RR:
            reasons.append("public_block_target_caution_rr_below_2_8")
    reasons.extend(_public_watchlist_actionability_blockers(symbol_result))
    reasons.extend(_public_watchlist_status_blockers(symbol_result, candidate=candidate))
    reasons.extend(_public_watchlist_active_rejection_blockers(symbol_result, candidate=candidate))
    reasons.extend(_public_watchlist_data_health_blockers(symbol_result, candidate=candidate))

    if not candidate.plan_complete and allowed_missing_gate is None and _text(symbol_result.rejection_reason) != NA:
        reasons.append("rejection_reason_present")
    if (
        not candidate.plan_complete
        and allowed_missing_gate is None
        and any(_text(reason) != NA for reason in symbol_result.rejection_reasons)
    ):
        reasons.append("rejection_reasons_present")

    missing = _public_watchlist_candidate_missing_fields(candidate) if context.public_watchlist_require_plan else ()
    if missing:
        reasons.extend(_public_watchlist_candidate_precise_missing_blockers(candidate))

    readiness_context = replace(context, min_rr=min_rr)
    reasons.extend(_watchlist_public_readiness_blockers(symbol_result, message, readiness_context, candidate=candidate))
    reasons.extend(_public_watchlist_target_integrity_blockers(symbol_result, message))

    if planned_rr is None:
        reasons.append("public_watchlist_missing_rr")
    elif planned_rr < min_rr:
        if target_caution:
            reasons.append("public_block_target_caution_rr_below_2_8")
        else:
            reasons.append(f"public_watchlist_rr_below_min:{_text(planned_rr)}<{_text(min_rr)}")

    if not failed_gate_codes and not target_caution and state_key != "actionable_a_grade" and _public_watchlist_actionability_state_key(symbol_result) not in PUBLIC_SIGNAL_ALLOWED_ACTIONABILITY_STATES:
        reasons.append("public_watchlist_missing_explicit_timing_gate")
    elif failed_gate_codes and allowed_missing_gate is None:
        malformed_gates = tuple(
            code
            for code in failed_gate_codes
            if classify_failed_gate_code(code) == PUBLIC_WATCHLIST_MALFORMED_FAILED_GATE_CLASS
        )
        missing_data_gates = tuple(
            code
            for code in failed_gate_codes
            if classify_failed_gate_code(code) == PUBLIC_WATCHLIST_MISSING_DATA_FAILED_GATE_CLASS
        )
        fatal_gates = tuple(
            code
            for code in failed_gate_codes
            if classify_failed_gate_code(code) == PUBLIC_WATCHLIST_FATAL_FAILED_GATE_CLASS
        )
        confirmed_rr_pending_gates = tuple(
            code
            for code in failed_gate_codes
            if classify_failed_gate_code(code) == CONFIRMED_SIGNAL_RR_PENDING
        )
        confirmed_quality_pending_gates = tuple(
            code
            for code in failed_gate_codes
            if classify_failed_gate_code(code) == CONFIRMED_SIGNAL_QUALITY_PENDING
        )
        timing_gates = tuple(
            code
            for code in failed_gate_codes
            if classify_failed_gate_code(code) == TIMING_CONFIRMATION_PENDING
        )
        non_regime_gates = tuple(
            code
            for code in failed_gate_codes
            if classify_failed_gate_code(code) == PUBLIC_WATCHLIST_NON_REGIME_FAILED_GATE_CLASS
        )
        unknown_gates = tuple(
            code
            for code in failed_gate_codes
            if classify_failed_gate_code(code) == PUBLIC_WATCHLIST_UNKNOWN_FAILED_GATE_CLASS
        )
        if malformed_gates:
            reasons.append("public_watchlist_malformed_failed_gate_diagnostics")
        if missing_data_gates:
            reasons.append(f"public_watchlist_missing_data_failed_gates={','.join(missing_data_gates)}")
        if fatal_gates:
            reasons.append(f"public_watchlist_fatal_failed_gates={','.join(fatal_gates)}")
        if non_regime_gates:
            reasons.append(f"public_watchlist_non_regime_failed_gates={','.join(non_regime_gates)}")
        if unknown_gates:
            reasons.append(f"public_watchlist_unknown_failed_gates={','.join(unknown_gates)}")
        if (
            REGIME_MARKET_CONDITION_PENDING in failed_gate_class_set
            and failed_gate_class_set != frozenset({REGIME_MARKET_CONDITION_PENDING})
        ):
            reasons.append(f"public_watchlist_conflicting_failed_gate_classes={','.join(failed_gate_classes)}")
        elif (
            TIMING_CONFIRMATION_PENDING in failed_gate_class_set
            and not failed_gate_class_set <= PUBLIC_WATCHLIST_ALLOWED_GATE_CLASSES
        ):
            reasons.append(f"public_watchlist_conflicting_failed_gate_classes={','.join(failed_gate_classes)}")
        elif not (
            malformed_gates
            or missing_data_gates
            or fatal_gates
            or timing_gates
            or confirmed_rr_pending_gates
            or confirmed_quality_pending_gates
            or non_regime_gates
            or unknown_gates
        ):
            reasons.append(f"public_watchlist_failed_gate_class_not_allowed={','.join(failed_gate_classes)}")

    return PublicWatchlistGateResult(
        allowed=not reasons,
        setup_id=None if setup_id == NA else setup_id,
        symbol=None if symbol == NA else symbol,
        state=state_key or NA,
        failed_gate_codes=failed_gate_codes,
        failed_gate_classes=failed_gate_classes,
        allowed_missing_gate=allowed_missing_gate,
        blocking_reasons=tuple(dict.fromkeys(reasons)),
        rr=float(planned_rr) if planned_rr is not None else None,
    )


def _public_watchlist_attempt_prefilter(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    context: TelegramEligibilityContext,
) -> PublicWatchlistPrefilterResult:
    candidate = _public_watchlist_candidate_from_symbol(symbol_result)
    reasons: list[str] = []
    if not context.public_watchlist_enabled:
        reasons.append("telegram_disabled")
    reasons.extend(_public_watchlist_candidate_precise_missing_blockers(candidate))

    state_key = candidate.lifecycle_state
    failed_gate_codes = _public_watchlist_failed_gate_codes(symbol_result, candidate=candidate)
    failed_gate_classes = _public_watchlist_failed_gate_classes(failed_gate_codes)
    rejected_state_allowed = _public_watchlist_rejected_state_allowed(
        symbol_result,
        candidate,
        failed_gate_codes,
        failed_gate_classes,
    )
    if state_key in PUBLIC_WATCHLIST_BLOCKED_STATE_KEYS or state_key in {
        "confirmed",
        "executing",
        "managing",
        "active",
    }:
        reasons.append("terminal_state")
    elif state_key in PUBLIC_WATCHLIST_REJECTED_STATE_KEYS and not rejected_state_allowed:
        reasons.append("terminal_state")
    elif not _public_watchlist_state_allowed(symbol_result, candidate, failed_gate_codes, failed_gate_classes):
        reasons.append("terminal_state")

    expiry = _watchlist_candidate_expiry_decision(symbol_result)
    if expiry.expired:
        reasons.append("public_watchlist_stale_or_expired")

    status_keys = set(_status_keys(symbol_result))
    if status_keys & {"scanned_no_setup", "rejected_by_scoring", "no_setup"} and not candidate.plan_complete:
        reasons.append("scanned_no_setup_without_candidate")

    data_health_flags = any(
        _sequence_or_single(getattr(symbol_result, field_name, ()))
        for field_name in (
            "missing_data",
            "unverified_data",
            "strategy_missing_data",
            "strategy_unverified_data",
            "derivatives_missing_data",
            "derivatives_unverified_data",
        )
    )
    if data_health_flags and _public_watchlist_candidate_missing_fields(candidate):
        reasons.extend(_public_watchlist_candidate_precise_missing_blockers(candidate))

    return PublicWatchlistPrefilterResult(
        passed=not reasons,
        blocking_reasons=tuple(dict.fromkeys(reasons)),
    )


def _confirmed_alert_attempt_prefilter(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    context: TelegramEligibilityContext,
) -> ConfirmedAlertPrefilterResult:
    reasons: list[str] = []
    state_key = _status_key(_public_gate_state(symbol_result))
    if state_key != "confirmed":
        reasons.append(f"lifecycle_state_not_confirmed:{state_key or 'missing'}")

    public_gate = _public_signal_gate_result(symbol_result, TelegramAlertType.SIGNAL_CONFIRMED, message)
    reasons.extend(public_gate.blocking_reasons)
    reasons.extend(
        _defensive_delivery_blockers(
            symbol_result,
            TelegramAlertType.SIGNAL_CONFIRMED,
            message,
            context,
        )
    )
    reasons.extend(_confirmed_alert_data_health_blockers(symbol_result))

    blocking_reasons = tuple(dict.fromkeys(reason for reason in reasons if _text(reason) != NA))
    return ConfirmedAlertPrefilterResult(
        passed=not blocking_reasons,
        blocking_reasons=blocking_reasons,
        reason_buckets=_confirmed_alert_prefilter_reason_buckets(blocking_reasons),
    )


def _confirmed_alert_data_health_blockers(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    score_result = symbol_result.score_result
    direct_values = (
        symbol_result.missing_data,
        symbol_result.unverified_data,
        symbol_result.strategy_missing_data,
        symbol_result.strategy_unverified_data,
        symbol_result.derivatives_missing_data,
        symbol_result.derivatives_unverified_data,
        getattr(score_result, "missing_data", ()) if score_result is not None else (),
        getattr(score_result, "unverified_data", ()) if score_result is not None else (),
    )
    if any(_sequence_or_single(value) for value in direct_values):
        return ("data_health_failed",)
    diagnostics = _representative_diagnostics(symbol_result)
    if any(_sequence_or_single(diagnostics.get(key)) for key in ("missing_data", "unverified_data")):
        return ("data_health_failed",)
    return ()


def _confirmed_alert_prefilter_reason_buckets(reasons: Sequence[str]) -> tuple[str, ...]:
    buckets: list[str] = []
    for reason in reasons:
        key = _status_key(reason)
        if "rejected_by_scoring" in key:
            buckets.append("rejected_by_scoring")
        elif (
            key.startswith("failed_confirmation_gate_scoring")
            or key.startswith("confirmed_current_failed_gate_scoring")
            or "failed_confirmation_gate_scoring" in key
            or (
                key.startswith(("failed_confirmation_gate:", "confirmed_current_failed_gate:"))
                and any(token in key for token in ("scoring", "score"))
            )
        ):
            buckets.append("failed_confirmation_gate_scoring")
        elif "watchlist_near_miss" in key:
            buckets.append("watchlist_near_miss_not_confirmed")
        elif "confirmed_grade_below_min" in key or "below_min_public_grade" in key:
            buckets.append("confirmed_grade_below_min")
        elif "trade_idea_missing" in key:
            buckets.append("trade_idea_missing")
        elif "technical_score_below_min" in key:
            buckets.append("technical_score_below_min")
        elif "opportunity_score_below_min" in key:
            buckets.append("opportunity_score_below_min")
        elif "confirmed_active_rejection_reason" in key:
            buckets.append("active_rejection_reason")
        elif "confirmed_active_invalidation" in key or "invalidation_contains_rejection_reason" in key:
            buckets.append("active_invalidation")
        elif "confirmed_missing_entry_zone" in key or "missing_required_fields_entry" in key:
            buckets.append("missing_entry_zone")
        elif "confirmed_missing_stop" in key or "missing_required_fields_stop_loss" in key:
            buckets.append("missing_stop")
        elif "planned_rr_below_min" in key or "confirmed_rr_below_min" in key or "confirmed_missing_rr" in key:
            buckets.append("rr_below_min")
        elif "data_health_failed" in key:
            buckets.append("data_health_failed")
    return tuple(dict.fromkeys(buckets))


def _public_watchlist_candidate_audit(
    symbol_result: ScannerSymbolResult,
    context: TelegramEligibilityContext,
    *,
    repository: SQLiteTelegramAlertAttemptRepository | None = None,
) -> PublicWatchlistCandidateAudit | None:
    candidate = _public_watchlist_candidate_from_symbol(symbol_result)
    explicit_source = _explicit_public_watchlist_source(symbol_result, candidate)
    transition = symbol_result.lifecycle_transition
    alert_type = _alert_type_for_transition(symbol_result, transition) if transition is not None else None
    if transition is None and not explicit_source:
        return None
    if alert_type != TelegramAlertType.WATCHLIST and not explicit_source:
        return None

    message = _telegram_signal_message_for_alert(symbol_result, TelegramAlertType.WATCHLIST, context)
    prefilter = _public_watchlist_attempt_prefilter(symbol_result, message, context)
    gate = _public_watchlist_gate_result(symbol_result, message, context)
    eligible = prefilter.passed and gate.allowed
    canonical_plan = _public_watchlist_canonical_plan(symbol_result, message)
    plan_hash = canonical_plan.plan_hash
    watchlist_signal_id = _public_watchlist_signal_id(symbol_result, message)
    cooldown_key = canonical_plan.plan_id
    prior_status = NA
    if repository is not None:
        prior_status = repository.public_watchlist_plan_attempt_status(
            symbol=symbol_result.symbol,
            direction=message.direction,
            cooldown_key=cooldown_key,
            signal_id=_signal_id(symbol_result),
        )
    failed_gate_codes = gate.failed_gate_codes
    active_fatal_reasons = _public_watchlist_fatal_reasons(
        symbol_result,
        message,
        candidate=candidate,
        failed_gate_codes=failed_gate_codes,
    )
    confirmed_only_pending_reasons = _public_watchlist_confirmed_only_pending_reasons(failed_gate_codes)
    reject_reasons = gate.blocking_reasons if prefilter.passed else prefilter.blocking_reasons
    public_decision = "eligible" if eligible else "blocked"
    return PublicWatchlistCandidateAudit(
        symbol=_symbol(message.symbol),
        eligible=eligible,
        field_prefilter_passed=prefilter.passed,
        first_seen_triggered_pre_confirmation=candidate.first_seen_triggered_pre_confirmation,
        state=candidate.lifecycle_state,
        core_lifecycle_state=candidate.lifecycle_state,
        derived_public_watchlist_state=_public_watchlist_presentation_state(candidate),
        reject_reasons=reject_reasons,
        active_fatal_reasons=active_fatal_reasons,
        confirmed_only_pending_reasons=confirmed_only_pending_reasons,
        watchlist_eligibility_result="pass" if eligible else "blocked",
        public_decision=public_decision,
        public_block_reason=_audit_reason_text(reject_reasons) if not eligible else NA,
        actionable_grade_reason=_actionable_grade_reason_for_symbol(symbol_result),
        confirmation_block_reason=_audit_reason_text(_confirmed_guard_blockers(symbol_result, context)),
        lifecycle_promotion_reason=_lifecycle_promotion_reason(symbol_result),
        plan_hash=plan_hash,
        public_watchlist_plan_id=canonical_plan.plan_id,
        side=canonical_plan.side,
        source_modes=canonical_plan.source_modes,
        raw_zone=f"{canonical_plan.raw_entry_low}-{canonical_plan.raw_entry_high}"
        if canonical_plan.raw_entry_low != NA and canonical_plan.raw_entry_high != NA
        else NA,
        normalized_zone=f"{canonical_plan.normalized_entry_low}-{canonical_plan.normalized_entry_high}"
        if canonical_plan.normalized_entry_low != NA and canonical_plan.normalized_entry_high != NA
        else NA,
        raw_invalidation=canonical_plan.raw_invalidation,
        normalized_invalidation=canonical_plan.normalized_invalidation,
        structural_identity_fields=dict(canonical_plan.structural_identity),
        previous_successful_alert_id=NA,
        dedupe_decision="prior_successful_alert_found"
        if prior_status == "sent"
        else "eligible_new_plan"
        if eligible
        else "blocked_before_send",
        material_change_reason=canonical_plan.material_change_reason,
        prior_public_alert_status=prior_status,
        rr=_text(candidate.potential_rr),
        grade=_text(candidate.grade),
        has_side_grade=candidate.side in {"long", "short"} and _text(candidate.grade) != NA,
        has_rr=_decimal_or_none(candidate.potential_rr) is not None,
        has_zone=_public_watchlist_has_zone(message),
        has_invalidation=_public_watchlist_has_invalidation(message),
        plan_complete=candidate.plan_complete,
        explicit_source=explicit_source,
        historical_rejection_only_ignored=_public_watchlist_historical_rejection_only_ignored(
            symbol_result,
            candidate=candidate,
        ),
    )

def _audit_reason_text(reasons: Sequence[Any]) -> str:
    cleaned = tuple(_text(reason) for reason in reasons if _text(reason) != NA)
    return ";".join(cleaned) if cleaned else NA


def _lifecycle_promotion_reason(symbol_result: ScannerSymbolResult) -> str:
    transition = symbol_result.lifecycle_transition
    if transition is None:
        return NA
    return _text(getattr(transition.reason, "value", transition.reason))


def _actionable_grade_reason_for_symbol(symbol_result: ScannerSymbolResult) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    actionability_state = _status_key(
        _first_non_na(
            getattr(symbol_result, "actionability_state", NA),
            getattr(lifecycle, "actionability_state", NA) if lifecycle is not None else NA,
            diagnostics.get("actionability_state"),
        )
    )
    if actionability_state in A_GRADE_PUBLIC_BLOCKED_ACTIONABILITY_STATES:
        return _text(
            _first_non_na(
                getattr(symbol_result, "final_block_reason", NA),
                getattr(lifecycle, "final_block_reason", NA) if lifecycle is not None else NA,
                diagnostics.get("final_block_reason"),
                actionability_state,
            )
        )
    for key in ("actionable_grade_reason", "actionable_a_grade_reason"):
        value = _text(diagnostics.get(key))
        if value != NA:
            return value
    if actionability_state == "a_grade_actionable" or _status_key(_lifecycle_state_text(symbol_result)) == "actionable_a_grade":
        return "lifecycle_state_actionable_a_grade"
    return NA


def _public_watchlist_duplicate_skip_reason(reason: Any) -> bool:
    key = _text(reason)
    return key == "skipped_duplicate_same_plan" or key.startswith(PUBLIC_WATCHLIST_DUPLICATE_SUCCESS_REASON)


def _public_watchlist_has_zone(message: TelegramSignalMessage) -> bool:
    return _decimal_pair_values(message.entry_low, message.entry_high) is not None or _decimal_pair_text(message.watch_zone) is not None


def _public_watchlist_has_invalidation(message: TelegramSignalMessage) -> bool:
    return _text(_first_non_na(message.watchlist_invalidation_reason, message.invalidation_reason, message.stop_loss)) != NA


def _public_watchlist_audit_summary(
    audits: Sequence[PublicWatchlistCandidateAudit],
) -> PublicWatchlistAuditSummary:
    candidates = tuple(audits)
    blocked_by_reason: dict[str, int] = {}
    for audit in candidates:
        reasons = audit.reject_reasons if not audit.eligible else ()
        if audit.eligible and audit.delivery_status not in {"", NA, "sent"}:
            reasons = (audit.skip_reason if audit.skip_reason != NA else audit.delivery_status,)
        if audit.historical_rejection_only_ignored:
            reasons = (*reasons, "historical_rejection_only_ignored")
        for reason in reasons:
            bucket = _public_watchlist_skip_bucket(reason)
            blocked_by_reason[bucket] = blocked_by_reason.get(bucket, 0) + 1
    field_prefilter_passed = sum(1 for audit in candidates if audit.field_prefilter_passed)
    eligible_first_seen = sum(
        1
        for audit in candidates
        if audit.eligible and audit.first_seen_triggered_pre_confirmation
    )
    eligible_watch_or_stalking = sum(
        1
        for audit in candidates
        if audit.eligible and not audit.first_seen_triggered_pre_confirmation
    )
    alerts_created = sum(
        1
        for audit in candidates
        if audit.delivery_status in {"sent", "failed", "skipped", "blocked", "blocked_repeat", "duplicate"}
        and audit.skip_reason != "no_send_attempt"
    )
    deduped = sum(
        1
        for audit in candidates
        if audit.skip_reason in {"public_watchlist_prior_plan_alert_exists", "duplicate"} or _public_watchlist_duplicate_skip_reason(audit.skip_reason)
        or audit.delivery_status == "duplicate"
    )
    cooldown = sum(1 for audit in candidates if audit.skip_reason == "public_watchlist_cooldown_active")
    return PublicWatchlistAuditSummary(
        source_candidates_seen=len(candidates),
        field_prefilter_passed=field_prefilter_passed,
        eligible_watch_or_stalking=eligible_watch_or_stalking,
        eligible_first_seen_triggered_pre_confirmation=eligible_first_seen,
        blocked_before_attempt=sum(1 for audit in candidates if not audit.field_prefilter_passed),
        blocked_after_attempt=sum(
            1
            for audit in candidates
            if audit.field_prefilter_passed
            and not audit.eligible
        ),
        blocked_by_reason=dict(sorted(blocked_by_reason.items())),
        near_miss_seen=len(candidates),
        near_miss_with_side_grade=sum(1 for audit in candidates if audit.has_side_grade),
        near_miss_with_rr=sum(1 for audit in candidates if audit.has_rr),
        near_miss_with_entry_zone=sum(1 for audit in candidates if audit.has_zone),
        near_miss_with_invalidation=sum(1 for audit in candidates if audit.has_invalidation),
        near_miss_plan_complete=sum(1 for audit in candidates if audit.plan_complete),
        public_watchlist_trade_ideas_created=eligible_watch_or_stalking + eligible_first_seen,
        public_watchlist_alerts_created=alerts_created,
        public_watchlist_sent=sum(1 for audit in candidates if audit.delivery_status == "sent"),
        complete_candidates_seen=sum(1 for audit in candidates if audit.plan_complete),
        eligible_by_watchlist_policy=eligible_watch_or_stalking + eligible_first_seen,
        blocked_by_fatal_reason=sum(1 for audit in candidates if audit.active_fatal_reasons and not audit.eligible),
        blocked_by_grade=sum(1 for audit in candidates if any(_public_watchlist_skip_bucket(reason) in {"missing_grade", "below_min_grade"} for reason in audit.reject_reasons)),
        blocked_by_rr=sum(1 for audit in candidates if any(_public_watchlist_skip_bucket(reason) in {"missing_rr", "rr_below_public_min"} for reason in audit.reject_reasons)),
        blocked_by_missing_zone=sum(1 for audit in candidates if any(_public_watchlist_skip_bucket(reason) == "missing_entry_zone" for reason in audit.reject_reasons)),
        blocked_by_missing_invalidation=sum(1 for audit in candidates if any(_public_watchlist_skip_bucket(reason) == "missing_invalidation" for reason in audit.reject_reasons)),
        allowed_confirmed_only_scoring_failure=sum(
            1
            for audit in candidates
            if any(
                classify_failed_gate_code(reason) in {CONFIRMED_SIGNAL_RR_PENDING, CONFIRMED_SIGNAL_QUALITY_PENDING}
                for reason in audit.confirmed_only_pending_reasons
            )
        ),
        watch_state_candidates=sum(1 for audit in candidates if audit.derived_public_watchlist_state == "WATCHLIST"),
        triggered_waiting_confirmation_candidates=sum(1 for audit in candidates if audit.derived_public_watchlist_state == "LIMIT ZONE HIT \u2014 WAITING CONFIRMATION"),
        rejected_core_but_watchlist_eligible=sum(1 for audit in candidates if audit.eligible and audit.core_lifecycle_state in PUBLIC_WATCHLIST_REJECTED_STATE_KEYS),
        candidates_with_prior_successful_alert=sum(1 for audit in candidates if audit.prior_public_alert_status == "sent"),
        candidates_without_prior_successful_alert=sum(1 for audit in candidates if audit.prior_public_alert_status != "sent"),
        attempts_created=alerts_created,
        deduped=deduped,
        cooldown=cooldown,
        candidates_considered=len(candidates),
        eligible=eligible_watch_or_stalking + eligible_first_seen,
        sent=sum(1 for audit in candidates if audit.delivery_status == "sent"),
        skipped_by_reason=dict(sorted(blocked_by_reason.items())),
        candidates=candidates,
    )


def _public_watchlist_skip_bucket(reason: Any) -> str:
    key = _status_key(reason)
    text = _text(reason).lower()
    if "missing_side" in key or "missing_direction" in key:
        return "missing_side"
    if "missing_grade" in key:
        return "missing_grade"
    if "below_min_public_grade" in key or "below_min_grade" in key or "grade_below" in key:
        return "below_min_grade"
    if any(token in key for token in ("missing_rr", "planned_rr_missing", "rr_missing")) or "planned_rr" in text and "missing" in text:
        return "missing_rr"
    if "rr_below" in key or "planned_rr_below_min" in key or "rr_below" in text:
        return "rr_below_public_min"
    if "entry_zone" in key or "limit_zone" in key:
        return "missing_entry_zone"
    if "invalidation" in key or "stop_loss" in key or "missing_stop" in key or "missing_sl" in key:
        return "missing_invalidation"
    if "target_inside_chop" in key:
        return "target_inside_chop"
    if "late_pullback" in key:
        return "late_pullback"
    if "no_edge" in key:
        return "no_edge"
    if "active_invalidation" in key:
        return "active_invalidation"
    if "active_rejection" in key or "structural_breakdown" in key or "body_acceptance_failure" in key:
        return "active_rejection"
    if "terminal_state" in key or "terminal_or_rejected" in key or "state_not_eligible" in key:
        return "terminal_state"
    if "expired" in key or "stale" in key or "entry_window_expired" in key or key == _status_key(WATCHLIST_EXPIRY_REASON):
        return "stale_or_expired"
    if "target_integrity" in key:
        return "target_inside_chop" if "target_inside_chop" in key else "target_integrity_failed"
    if "scanned_no_setup_without_candidate" in key:
        return "scanned_no_setup_without_candidate"
    if "historical_rejection_only_ignored" in key:
        return "historical_rejection_only_ignored"
    if "prior_plan_alert_exists" in key or "duplicate" in key:
        return "deduped"
    if "cooldown" in key:
        return "cooldown"
    if "disabled" in key:
        return "telegram_disabled"
    if "dry_run" in key:
        return "dry_run"
    if "channel" in key or "chat" in key:
        return "channel_missing"
    return key or "unknown"


def _log_public_watchlist_audit(summary: PublicWatchlistAuditSummary) -> None:
    if summary.source_candidates_seen == 0:
        return
    logger.info(
        "public_watchlist_audit source_candidates_seen=%s field_prefilter_passed=%s "
        "eligible_watch_or_stalking=%s eligible_first_seen_triggered_pre_confirmation=%s sent=%s "
        "blocked_before_attempt=%s blocked_after_attempt=%s blocked_by_reason=%s bridge=%s reconciliation=%s dedupe=%s hard_dedupe=%s candidates=%s",
        summary.source_candidates_seen,
        summary.field_prefilter_passed,
        summary.eligible_watch_or_stalking,
        summary.eligible_first_seen_triggered_pre_confirmation,
        summary.sent,
        summary.blocked_before_attempt,
        summary.blocked_after_attempt,
        dict(summary.blocked_by_reason),
        dict(summary.public_watchlist_bridge),
        dict(summary.public_watchlist_reconciliation_audit),
        dict(summary.public_watchlist_dedupe_audit),
        dict(summary.public_watchlist_hard_dedupe_audit),
        tuple(
            {
                "symbol": audit.symbol,
                "public_watchlist_eligible": audit.eligible,
                "public_watchlist_field_prefilter_passed": audit.field_prefilter_passed,
                "public_watchlist_first_seen_triggered_pre_confirmation": audit.first_seen_triggered_pre_confirmation,
                "public_watchlist_state": audit.state,
                "public_watchlist_core_lifecycle_state": audit.core_lifecycle_state,
                "public_watchlist_derived_state": audit.derived_public_watchlist_state,
                "public_watchlist_active_fatal_reasons": audit.active_fatal_reasons,
                "public_watchlist_confirmed_only_pending_reasons": audit.confirmed_only_pending_reasons,
                "public_watchlist_eligibility_result": audit.watchlist_eligibility_result,
                "public_decision": audit.public_decision,
                "public_block_reason": audit.public_block_reason,
                "actionable_grade_reason": audit.actionable_grade_reason,
                "confirmation_block_reason": audit.confirmation_block_reason,
                "lifecycle_promotion_reason": audit.lifecycle_promotion_reason,
                "public_watchlist_plan_hash": audit.plan_hash,
                "public_watchlist_plan_id": audit.public_watchlist_plan_id,
                "public_watchlist_side": audit.side,
                "public_watchlist_source_modes": audit.source_modes,
                "public_watchlist_raw_zone": audit.raw_zone,
                "public_watchlist_normalized_zone": audit.normalized_zone,
                "public_watchlist_raw_invalidation": audit.raw_invalidation,
                "public_watchlist_normalized_invalidation": audit.normalized_invalidation,
                "public_watchlist_structural_identity_fields": dict(audit.structural_identity_fields),
                "public_watchlist_previous_successful_alert_id": audit.previous_successful_alert_id,
                "public_watchlist_dedupe_decision": audit.dedupe_decision,
                "public_watchlist_material_change_reason": audit.material_change_reason,
                "public_watchlist_prior_public_alert_status": audit.prior_public_alert_status,
                "public_watchlist_explicit_source": audit.explicit_source,
                "public_watchlist_reject_reasons": audit.reject_reasons,
                "public_watchlist_rr": audit.rr,
                "public_watchlist_grade": audit.grade,
                "public_watchlist_has_side_grade": audit.has_side_grade,
                "public_watchlist_has_rr": audit.has_rr,
                "public_watchlist_has_zone": audit.has_zone,
                "public_watchlist_has_invalidation": audit.has_invalidation,
                "public_watchlist_plan_complete": audit.plan_complete,
                "public_watchlist_historical_rejection_only_ignored": audit.historical_rejection_only_ignored,
                "public_watchlist_delivery_status": audit.delivery_status,
                "public_watchlist_skip_reason": audit.skip_reason,
            }
            for audit in summary.candidates
        ),
    )


def _log_confirmed_alert_audit(summary: ConfirmedAlertAuditSummary) -> None:
    if summary.confirmed_candidates_seen == 0:
        return
    logger.info(
        "confirmed_alert_audit public_signal_policy=%s confirmed_candidates_seen=%s "
        "confirmed_prefilter_passed=%s confirmed_policy_disabled=%s "
        "signal_confirmed_attempts_created=%s signal_confirmed_sent=%s "
        "policy_disabled_by_reason=%s blocked_before_attempt_by_reason=%s",
        summary.public_signal_policy,
        summary.confirmed_candidates_seen,
        summary.confirmed_prefilter_passed,
        summary.confirmed_policy_disabled,
        summary.signal_confirmed_attempts_created,
        summary.signal_confirmed_sent,
        dict(summary.policy_disabled_by_reason),
        dict(summary.blocked_before_attempt_by_reason),
    )


def _public_watchlist_failed_gate_codes(
    symbol_result: ScannerSymbolResult,
    *,
    candidate: PublicWatchlistCandidate | None = None,
) -> tuple[str, ...]:
    diagnostics = _representative_diagnostics(symbol_result)
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    lifecycle = symbol_result.lifecycle_state
    values: list[Any] = [
        _field(intelligence, "primary_failed_gate"),
        *(_diagnostic_texts(diagnostics, ACTIVE_FAILED_GATE_KEYS)),
        diagnostics.get("first_failed_gate"),
        diagnostics.get("failed_gate"),
        getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
    ]
    values.extend(_sequence_or_single(diagnostics.get("gates_failed")))
    values.extend(_sequence_or_single(diagnostics.get("failed_gates")))

    codes = tuple(dict.fromkeys(code for value in values if (code := normalize_failed_gate_code(value))))
    stage_code = normalize_failed_gate_code(symbol_result.rejection_stage)
    if stage_code and stage_code != "regime":
        candidate_complete = candidate is not None and candidate.plan_complete
        stage_is_final_scanner_status = stage_code in {
            "no_setup",
            "rejected_by_scoring",
            "scanned_no_setup",
            "scoring",
        }
        if not (candidate_complete and stage_is_final_scanner_status and codes):
            codes = tuple(dict.fromkeys((*codes, stage_code)))
    if _public_watchlist_target_caution_actionable(symbol_result):
        codes = tuple(
            code
            for code in codes
            if code != "target_inside_chop"
        )
    if candidate is not None:
        if candidate.first_seen_triggered_pre_confirmation and not codes:
            codes = ("first_seen_triggered_pre_confirmation",)
        has_allowed_source_with_zone = _public_watchlist_candidate_has_zone(candidate) and any(
            code in PUBLIC_WATCHLIST_TIMING_PENDING_GATE_CODES
            or code in PUBLIC_WATCHLIST_CONFIRMED_RR_GATE_CODES
            or code in PUBLIC_WATCHLIST_CONFIRMED_ONLY_GATE_CODES
            for code in codes
            if code not in {"no_ob_or_fvg_zone", "challenge_limit_entry_missing"}
        )
        if has_allowed_source_with_zone:
            codes = tuple(
                code
                for code in codes
                if code not in {"no_ob_or_fvg_zone", "challenge_limit_entry_missing"}
            )
    return codes


def _public_watchlist_failed_gate_classes(failed_gate_codes: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            failed_gate_class
            for code in failed_gate_codes
            if (failed_gate_class := classify_failed_gate_code(code))
        )
    )


def _public_watchlist_allowed_missing_gate(failed_gate_classes: Sequence[str]) -> str | None:
    failed_gate_class_set = frozenset(failed_gate_classes)
    if failed_gate_class_set == frozenset({REGIME_MARKET_CONDITION_PENDING}):
        return REGIME_MARKET_CONDITION_PENDING
    if failed_gate_class_set and failed_gate_class_set <= PUBLIC_WATCHLIST_ALLOWED_GATE_CLASSES:
        if TIMING_CONFIRMATION_PENDING in failed_gate_class_set:
            return TIMING_CONFIRMATION_PENDING
        if CONFIRMED_SIGNAL_RR_PENDING in failed_gate_class_set:
            return CONFIRMED_SIGNAL_RR_PENDING
        if CONFIRMED_SIGNAL_QUALITY_PENDING in failed_gate_class_set:
            return CONFIRMED_SIGNAL_QUALITY_PENDING
    return None


def _public_watchlist_confirmed_only_pending_reasons(failed_gate_codes: Sequence[str]) -> tuple[str, ...]:
    allowed_classes = {
        TIMING_CONFIRMATION_PENDING,
        CONFIRMED_SIGNAL_RR_PENDING,
        CONFIRMED_SIGNAL_QUALITY_PENDING,
    }
    return tuple(
        dict.fromkeys(
            code
            for code in failed_gate_codes
            if classify_failed_gate_code(code) in allowed_classes
        )
    )


def _public_watchlist_allowed_confirmed_only_scoring_failure(failed_gate_codes: Sequence[str]) -> bool:
    return any(
        classify_failed_gate_code(code) in {CONFIRMED_SIGNAL_RR_PENDING, CONFIRMED_SIGNAL_QUALITY_PENDING}
        for code in failed_gate_codes
    )


def _public_watchlist_fatal_reasons(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage | None,
    *,
    candidate: PublicWatchlistCandidate,
    failed_gate_codes: Sequence[str] | None = None,
) -> tuple[str, ...]:
    codes = tuple(failed_gate_codes or _public_watchlist_failed_gate_codes(symbol_result, candidate=candidate))
    reasons = [
        f"public_watchlist_fatal_failed_gate:{code}"
        for code in codes
        if classify_failed_gate_code(code) == PUBLIC_WATCHLIST_FATAL_FAILED_GATE_CLASS
    ]
    reasons.extend(_public_watchlist_active_rejection_blockers(symbol_result, candidate=candidate))
    reasons.extend(_public_watchlist_data_health_blockers(symbol_result, candidate=candidate))
    if message is not None:
        reasons.extend(_public_watchlist_target_integrity_blockers(symbol_result, message))
    return tuple(dict.fromkeys(reason for reason in reasons if _text(reason) != NA))


def _public_watchlist_rejected_state_allowed(
    symbol_result: ScannerSymbolResult,
    candidate: PublicWatchlistCandidate,
    failed_gate_codes: Sequence[str],
    failed_gate_classes: Sequence[str],
) -> bool:
    if candidate.lifecycle_state not in PUBLIC_WATCHLIST_REJECTED_STATE_KEYS:
        return False
    if not candidate.plan_complete:
        return False
    if not _explicit_public_watchlist_source(symbol_result, candidate):
        return False
    if not failed_gate_codes:
        return False
    confirmed_only_classes = {CONFIRMED_SIGNAL_RR_PENDING, CONFIRMED_SIGNAL_QUALITY_PENDING}
    return bool(failed_gate_classes) and frozenset(failed_gate_classes) <= confirmed_only_classes


def _public_watchlist_state_allowed(
    symbol_result: ScannerSymbolResult,
    candidate: PublicWatchlistCandidate,
    failed_gate_codes: Sequence[str],
    failed_gate_classes: Sequence[str],
) -> bool:
    state_key = candidate.lifecycle_state
    if state_key in PUBLIC_WATCHLIST_ELIGIBLE_STATE_KEYS:
        return True
    if state_key == PUBLIC_WATCHLIST_FIRST_SEEN_TRIGGERED_STATE_KEY:
        if _public_watchlist_target_caution_actionable(symbol_result):
            return _public_watchlist_target_caution_confirmation_ready(symbol_result)
        return candidate.first_seen_triggered_pre_confirmation
    return _public_watchlist_rejected_state_allowed(symbol_result, candidate, failed_gate_codes, failed_gate_classes)


def _public_watchlist_presentation_state(candidate: PublicWatchlistCandidate) -> str:
    if candidate.first_seen_triggered_pre_confirmation:
        return "LIMIT ZONE HIT \u2014 WAITING CONFIRMATION"
    return "WATCHLIST"


def _symbol_display_statuses(symbol_result: ScannerSymbolResult) -> set[str]:
    try:
        display = build_symbol_display(symbol_result)
    except Exception:
        return set()
    values = (
        getattr(display, "display_status", NA),
        getattr(display, "display_bucket", NA),
        getattr(display, "readiness_label", NA),
        getattr(display, "failed_stage", NA),
        getattr(display, "failed_gate", NA),
    )
    return {_status_key(value) for value in values if _status_key(value)}


def _explicit_public_watchlist_source(
    symbol_result: ScannerSymbolResult,
    candidate: PublicWatchlistCandidate | None = None,
) -> bool:
    diagnostics = _representative_diagnostics(symbol_result)
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    display_statuses = _symbol_display_statuses(symbol_result)
    status_keys = set(_status_keys(symbol_result))
    if symbol_result.regime_blocked:
        return False
    state_key = _status_key(_public_gate_state(symbol_result))
    if state_key in PUBLIC_WATCHLIST_ELIGIBLE_STATE_KEYS or state_key == PUBLIC_WATCHLIST_FIRST_SEEN_TRIGGERED_STATE_KEY:
        return True
    if _setup_quality_state_key(symbol_result) == "watchlist_near_miss":
        return True
    if _status_key(diagnostics.get("draft_type")) in {"near_miss", "watchlist", "watchlist_near_miss"}:
        return True
    if intelligence is not None:
        return True
    if display_statuses & {"near_miss", "watchlist_near_miss", "watchlist"}:
        return True
    if status_keys & {"near_miss"}:
        return True
    if _explicit_watchlist_candidate(symbol_result):
        return True
    if status_keys & {"scanned_no_setup", "rejected_by_scoring", "no_setup"}:
        candidate = candidate or _public_watchlist_candidate_from_symbol(symbol_result)
        failed_gate_codes = _public_watchlist_failed_gate_codes(symbol_result, candidate=candidate)
        return bool(
            candidate.plan_complete
            and failed_gate_codes
            and _public_watchlist_confirmed_only_pending_reasons(failed_gate_codes)
        )
    return False


def _public_watchlist_setup_type(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    lifecycle = symbol_result.lifecycle_state
    return _text(
        _first_non_na(
            message.mode,
            getattr(lifecycle, "mode", NA) if lifecycle is not None else NA,
            diagnostics.get("mode"),
            diagnostics.get("setup_type"),
            _field(setup, "mode"),
            _field(setup, "setup_type"),
            getattr(trade_idea, "setup_type", NA) if trade_idea is not None else NA,
            symbol_result.strategy_name,
        )
    )


PUBLIC_WATCHLIST_STRUCTURE_IDENTITY_KEYS = (
    "sweep_structure_id",
    "sweep_id",
    "sweep_index",
    "sweep_candle_index",
    "pullback_sweep_candle_index",
    "bos_structure_id",
    "bos_id",
    "bos_index",
    "bos_candle_index",
    "choch_structure_id",
    "choch_id",
    "choch_index",
    "choch_candle_index",
    "bos_choch_structure_id",
    "bos_choch_id",
    "bos_choch_index",
    "bos_choch_candle_index",
    "pullback_bos_choch_candle_index",
    "displacement_start_index",
    "displacement_end_index",
    "pullback_zone_id",
    "zone_id",
    "ob_zone_id",
    "fvg_zone_id",
)


def _public_watchlist_plan_id(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> str:
    return _public_watchlist_canonical_plan(symbol_result, message).plan_id


def _public_watchlist_event_key(plan_id: Any, event_type: Any = PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE) -> str:
    normalized_plan_id = _text(plan_id)
    normalized_event_type = _status_key(event_type) or PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE
    if normalized_plan_id == NA:
        return NA
    return f"{normalized_plan_id}|{normalized_event_type}"


def _public_watchlist_record_plan_fields(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage | None,
) -> dict[str, str]:
    if alert_type != TelegramAlertType.WATCHLIST or message is None:
        return {}
    plan = _public_watchlist_canonical_plan(symbol_result, message)
    event_key = _public_watchlist_event_key(plan.plan_id, PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE)
    return {
        "public_watchlist_plan_id": plan.plan_id,
        "public_watchlist_event_key": event_key,
        "public_alert_event_type": PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
        "normalized_entry_zone_low": plan.normalized_entry_low,
        "normalized_entry_zone_high": plan.normalized_entry_high,
        "normalized_invalidation": plan.normalized_invalidation,
    }


def _public_watchlist_canonical_plan(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
) -> PublicWatchlistPlanIdentity:
    symbol = _symbol(_first_non_na(message.symbol, symbol_result.symbol))
    side = _status_key(message.direction)
    if symbol == NA or side not in {"long", "short"}:
        return PublicWatchlistPlanIdentity()
    entry_low = _decimal_or_none(message.entry_low)
    entry_high = _decimal_or_none(message.entry_high)
    invalidation = _first_decimal(message.stop_loss, message.watchlist_invalidation_reason, message.invalidation_reason)
    if entry_low is None or entry_high is None or invalidation is None:
        return PublicWatchlistPlanIdentity(symbol=symbol, side=side)
    if entry_low > entry_high:
        entry_low, entry_high = entry_high, entry_low
    setup_family = _public_watchlist_setup_family(symbol_result, message)
    if setup_family == NA:
        return PublicWatchlistPlanIdentity(symbol=symbol, side=side)
    tick_size = _public_watchlist_tick_size(symbol_result, message, (entry_low, entry_high, invalidation))
    normalized_entry_low = _public_watchlist_normalized_price(entry_low, tick_size)
    normalized_entry_high = _public_watchlist_normalized_price(entry_high, tick_size)
    invalidation_quantum = _public_watchlist_material_invalidation_quantum(entry_low, entry_high, tick_size)
    normalized_invalidation = _public_watchlist_quantized_price(invalidation, invalidation_quantum)
    structural_identity = _public_watchlist_structural_identity(symbol_result, message)
    if structural_identity:
        identity_parts = (
            PUBLIC_WATCHLIST_PLAN_ID_VERSION,
            symbol,
            side,
            setup_family,
            *(f"{key}={value}" for key, value in structural_identity),
        )
    else:
        identity_parts = (
            PUBLIC_WATCHLIST_PLAN_ID_VERSION,
            symbol,
            side,
            setup_family,
            normalized_entry_low,
            normalized_entry_high,
            normalized_invalidation,
        )
    plan_hash = hashlib.sha256("|".join(_text(part) for part in identity_parts).encode("utf-8")).hexdigest()[:20]
    return PublicWatchlistPlanIdentity(
        plan_id=f"{symbol}|{side}|{plan_hash}",
        plan_hash=plan_hash,
        symbol=symbol,
        side=side,
        setup_family=setup_family,
        source_modes=_public_watchlist_source_modes(symbol_result, message),
        structural_identity=structural_identity,
        raw_entry_low=_text(entry_low),
        raw_entry_high=_text(entry_high),
        raw_invalidation=_text(invalidation),
        normalized_entry_low=normalized_entry_low,
        normalized_entry_high=normalized_entry_high,
        normalized_invalidation=normalized_invalidation,
        tick_size=_text(tick_size),
        material_change_reason="same_plan_exact" if structural_identity else "same_plan_within_price_tolerance",
    )


def _public_watchlist_setup_family(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    raw = _first_non_na(
        diagnostics.get("setup_type"),
        _field(setup, "setup_type"),
        getattr(trade_idea, "setup_type", NA) if trade_idea is not None else NA,
        symbol_result.strategy_name,
        message.mode,
        getattr(symbol_result.lifecycle_state, "mode", NA) if symbol_result.lifecycle_state is not None else NA,
        diagnostics.get("mode"),
        _field(setup, "mode"),
    )
    key = _status_key(raw)
    if key in {"scalp", "swing", "challenge"}:
        key = _status_key(symbol_result.strategy_name) or "liquidity_grab_pullback"
    for suffix in ("_scalp", "_swing", "_challenge"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return key or NA


def _public_watchlist_source_modes(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> tuple[str, ...]:
    values: list[Any] = [
        message.mode,
        getattr(symbol_result.lifecycle_state, "mode", NA) if symbol_result.lifecycle_state is not None else NA,
        *symbol_result.valid_strategy_modes,
        *symbol_result.rejected_strategy_modes,
    ]
    diagnostics = _representative_diagnostics(symbol_result)
    values.append(diagnostics.get("mode"))
    return tuple(dict.fromkeys(key for value in values if (key := _status_key(value))))


def _public_watchlist_structural_identity(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
) -> tuple[tuple[str, str], ...]:
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    lifecycle = symbol_result.lifecycle_state
    sources: tuple[Any, ...] = (diagnostics, setup, trade_idea, lifecycle, message)
    pairs: list[tuple[str, str]] = []
    for key in PUBLIC_WATCHLIST_STRUCTURE_IDENTITY_KEYS:
        value = _first_non_na(*(_field(source, key) for source in sources))
        text = _text(value)
        if text != NA:
            pairs.append((key, text))
    return tuple(dict.fromkeys(pairs))


def _public_watchlist_tick_size(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    prices: Sequence[Decimal],
) -> Decimal:
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    sources: tuple[Any, ...] = (diagnostics, setup, symbol_result.trade_idea, message)
    for key in ("tick_size", "price_tick_size", "exchange_tick_size", "symbol_tick_size", "min_price_increment"):
        value = _first_non_na(*(_field(source, key) for source in sources))
        parsed = _decimal_or_none(value)
        if parsed is not None and parsed > 0:
            return parsed
    for key in ("price_precision", "price_decimals", "quote_precision"):
        value = _first_non_na(*(_field(source, key) for source in sources))
        precision = _integer_or_none(value)
        if precision is not None and precision >= 0:
            return Decimal(1).scaleb(-precision)
    return _public_watchlist_tick_size_from_prices(prices)


def _public_watchlist_tick_size_from_prices(prices: Sequence[Decimal]) -> Decimal:
    exponents = [price.as_tuple().exponent for price in prices if price.is_finite()]
    if exponents:
        return Decimal(1).scaleb(min(exponents))
    return Decimal("0.00000001")


def _public_watchlist_normalized_price(value: Decimal, tick_size: Decimal) -> str:
    tick = tick_size if tick_size > 0 else Decimal("0.00000001")
    quantum = tick * PUBLIC_WATCHLIST_PRICE_TOLERANCE_TICKS
    return _public_watchlist_quantized_price(value, quantum)


def _public_watchlist_quantized_price(value: Decimal, quantum: Decimal) -> str:
    normalized_quantum = quantum if quantum > 0 else Decimal("0.00000001")
    normalized = (value / normalized_quantum).to_integral_value(rounding=ROUND_HALF_UP) * normalized_quantum
    return _text(normalized)


def _public_watchlist_material_invalidation_quantum(
    entry_low: Decimal,
    entry_high: Decimal,
    tick_size: Decimal,
) -> Decimal:
    tick = tick_size if tick_size > 0 else Decimal("0.00000001")
    tick_quantum = tick * PUBLIC_WATCHLIST_PRICE_TOLERANCE_TICKS
    zone_width = abs(entry_high - entry_low)
    zone_quantum = zone_width * PUBLIC_WATCHLIST_ZONE_TOLERANCE_MULTIPLIER
    return max(tick_quantum, zone_quantum)

def _integer_or_none(value: Any) -> int | None:
    number = _decimal_or_none(value)
    if number is None:
        return None
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return None


def _public_watchlist_plan_hash(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> str:
    return _public_watchlist_canonical_plan(symbol_result, message).plan_hash


def _public_watchlist_signal_id(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> str:
    plan = _public_watchlist_canonical_plan(symbol_result, message)
    if plan.plan_hash == NA or plan.symbol == NA:
        return _signal_id(symbol_result)
    return f"{plan.symbol}-WATCH-{plan.plan_hash}"


def _signal_id_for_alert(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage | None,
) -> str:
    if alert_type == TelegramAlertType.WATCHLIST and message is not None:
        return _public_watchlist_signal_id(symbol_result, message)
    return _signal_id(symbol_result)


def _delivery_signal_id_for_decision(
    symbol_result: ScannerSymbolResult,
    decision: TelegramAlertDecision,
) -> str:
    return _signal_id_for_alert(symbol_result, decision.alert_type, decision.message)

def _public_watchlist_legacy_record_matches_plan(
    record: TelegramAlertAttemptRecord,
    plan: PublicWatchlistPlanIdentity,
) -> tuple[bool, bool]:
    if _symbol(record.symbol) != plan.symbol or _status_key(record.direction) != plan.side:
        return False, False
    record_plan_id = _text(record.public_watchlist_plan_id)
    if record_plan_id != NA and record_plan_id == plan.plan_id:
        return True, False

    current_low = _decimal_or_none(plan.raw_entry_low)
    current_high = _decimal_or_none(plan.raw_entry_high)
    current_invalidation = _decimal_or_none(plan.raw_invalidation)
    prior_low = _decimal_or_none(record.entry_low)
    prior_high = _decimal_or_none(record.entry_high)
    prior_invalidation = _decimal_or_none(record.stop_loss)
    if None in {current_low, current_high, current_invalidation, prior_low, prior_high, prior_invalidation}:
        return False, False
    assert current_low is not None
    assert current_high is not None
    assert current_invalidation is not None
    assert prior_low is not None
    assert prior_high is not None
    assert prior_invalidation is not None
    current_zone = (min(current_low, current_high), max(current_low, current_high))
    prior_zone = (min(prior_low, prior_high), max(prior_low, prior_high))
    tolerance = _public_watchlist_material_price_tolerance(
        plan,
        (current_zone[0], current_zone[1], current_invalidation, prior_zone[0], prior_zone[1], prior_invalidation),
    )
    zones_overlap = max(current_zone[0], prior_zone[0]) <= min(current_zone[1], prior_zone[1]) + tolerance
    invalidation_matches = abs(current_invalidation - prior_invalidation) <= tolerance
    if not zones_overlap or not invalidation_matches:
        return False, False

    normalized_matches = (
        _public_watchlist_quantized_price(prior_zone[0], tolerance) == plan.normalized_entry_low
        and _public_watchlist_quantized_price(prior_zone[1], tolerance) == plan.normalized_entry_high
        and _public_watchlist_quantized_price(prior_invalidation, tolerance) == plan.normalized_invalidation
    )
    jitter_matched = not normalized_matches or any(
        (
            abs(current_zone[0] - prior_zone[0]) > 0,
            abs(current_zone[1] - prior_zone[1]) > 0,
            abs(current_invalidation - prior_invalidation) > 0,
        )
    )
    return True, jitter_matched


def _public_watchlist_material_price_tolerance(
    plan: PublicWatchlistPlanIdentity,
    prices: Sequence[Decimal],
) -> Decimal:
    tick = _decimal_or_none(plan.tick_size)
    if tick is None or tick <= 0:
        tick = _public_watchlist_tick_size_from_prices(tuple(price for price in prices if price.is_finite()))
    tick_tolerance = tick * PUBLIC_WATCHLIST_PRICE_TOLERANCE_TICKS
    low = _decimal_or_none(plan.raw_entry_low)
    high = _decimal_or_none(plan.raw_entry_high)
    if low is None or high is None:
        return tick_tolerance
    zone_tolerance = abs(high - low) * PUBLIC_WATCHLIST_ZONE_TOLERANCE_MULTIPLIER
    return max(tick_tolerance, zone_tolerance)

def _public_watchlist_plan_row_matches(row: sqlite3.Row, *, cooldown_key: str, signal_id: str | None = None) -> bool:
    normalized_signal_id = _identity(signal_id)
    if normalized_signal_id != NA and _identity(row["signal_id"]) == normalized_signal_id:
        return True
    normalized_key = _text(cooldown_key)
    if normalized_key == NA:
        return False
    row_plan_id = _text(_row_value(row, "public_watchlist_plan_id"))
    if row_plan_id != NA and row_plan_id == normalized_key:
        return True
    return _public_watchlist_cooldown_key_from_row(row) == normalized_key

def normalize_failed_gate_code(value: Any) -> str:
    if _malformed_failed_gate_value(value):
        return PUBLIC_WATCHLIST_MALFORMED_FAILED_GATE_CLASS.lower()
    return _failed_gate_code(value)


def classify_failed_gate_code(value: Any) -> str:
    code = normalize_failed_gate_code(value)
    if not code:
        return ""
    if (
        ("technical_score" in code and "below" in code)
        or ("opportunity_score" in code and "below" in code)
        or ("trust_meter" in code and "below" in code)
        or code in {"scoring", "rejected_by_scoring"}
    ):
        return CONFIRMED_SIGNAL_QUALITY_PENDING
    if "rr" in code and "below" in code and "public_watchlist" not in code:
        return CONFIRMED_SIGNAL_RR_PENDING
    if code in PUBLIC_WATCHLIST_REGIME_PENDING_GATE_CODES:
        return REGIME_MARKET_CONDITION_PENDING
    if code in PUBLIC_WATCHLIST_TIMING_PENDING_GATE_CODES:
        return TIMING_CONFIRMATION_PENDING
    if code in PUBLIC_WATCHLIST_MISSING_DATA_GATE_CODES:
        return PUBLIC_WATCHLIST_MISSING_DATA_FAILED_GATE_CLASS
    if code == PUBLIC_WATCHLIST_MALFORMED_FAILED_GATE_CLASS.lower():
        return PUBLIC_WATCHLIST_MALFORMED_FAILED_GATE_CLASS
    if code in PUBLIC_WATCHLIST_CONFIRMED_RR_GATE_CODES:
        return CONFIRMED_SIGNAL_RR_PENDING
    if code in PUBLIC_WATCHLIST_CONFIRMED_ONLY_GATE_CODES:
        return CONFIRMED_SIGNAL_QUALITY_PENDING
    if code in PUBLIC_WATCHLIST_FATAL_GATE_CODES:
        return PUBLIC_WATCHLIST_FATAL_FAILED_GATE_CLASS
    if code in PUBLIC_WATCHLIST_KNOWN_NON_REGIME_FAILED_GATES:
        return PUBLIC_WATCHLIST_NON_REGIME_FAILED_GATE_CLASS
    return PUBLIC_WATCHLIST_UNKNOWN_FAILED_GATE_CLASS


def _malformed_failed_gate_value(value: Any) -> bool:
    if value is None or value == NA:
        return False
    if isinstance(value, Mapping):
        return True
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _failed_gate_code(value: Any) -> str:
    key = _status_key(value)
    if key in {"", "na", "n_a", "none", "null", "nan", "pass", "passed", "true", "false"}:
        return ""
    return key


def _public_watchlist_missing_required_fields(
    message: TelegramSignalMessage,
    *,
    require_entry_zone: bool = True,
    require_invalidation: bool = True,
) -> tuple[str, ...]:
    missing: list[str] = []
    if _text(message.signal_id) == NA:
        missing.append("signal_id")
    if _text(message.symbol) == NA:
        missing.append("symbol")
    if _status_key(message.direction) not in {"long", "short"}:
        missing.append("direction")
    if _text(message.mode) == NA:
        missing.append("setup_type")
    if require_entry_zone and _decimal_pair_values(message.entry_low, message.entry_high) is None and _decimal_pair_text(message.watch_zone) is None:
        missing.append("entry_zone")
    if _decimal_or_none(message.stop_loss) is None:
        missing.append("stop_loss")
    if _decimal_or_none(message.tp1) is None:
        missing.append("tp1")
    if _decimal_or_none(message.planned_rr) is None:
        missing.append("planned_rr")
    if require_invalidation and _text(_first_non_na(message.watchlist_invalidation_reason, message.invalidation_reason)) == NA:
        missing.append("invalidation")
    return tuple(missing)


def _public_watchlist_missing_identity_fields(
    message: TelegramSignalMessage,
    *,
    require_invalidation: bool = True,
) -> tuple[str, ...]:
    missing: list[str] = []
    if _text(message.signal_id) == NA:
        missing.append("signal_id")
    if _text(message.symbol) == NA:
        missing.append("symbol")
    if _status_key(message.direction) not in {"long", "short"}:
        missing.append("direction")
    if _text(message.mode) == NA:
        missing.append("setup_type")
    if require_invalidation and _text(_first_non_na(message.watchlist_invalidation_reason, message.invalidation_reason)) == NA:
        missing.append("invalidation")
    return tuple(missing)


def _public_setup_quality_score_decimal(symbol_result: ScannerSymbolResult) -> Decimal | None:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    setup_quality = symbol_result.setup_quality
    return _first_decimal(
        getattr(setup_quality, "quality_score", NA),
        diagnostics.get("setup_quality_score"),
        diagnostics.get("quality_score"),
        getattr(lifecycle, "quality_score", NA) if lifecycle is not None else NA,
    )

def _public_setup_quality_score_decimal(symbol_result: ScannerSymbolResult) -> Decimal | None:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    setup_quality = symbol_result.setup_quality
    return _first_decimal(
        getattr(setup_quality, "quality_score", NA),
        diagnostics.get("setup_quality_score"),
        diagnostics.get("quality_score"),
        getattr(lifecycle, "quality_score", NA) if lifecycle is not None else NA,
    )

def _public_watchlist_v1_blockers(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    context: TelegramEligibilityContext,
    *,
    candidate: PublicWatchlistCandidate,
) -> tuple[str, ...]:
    reasons: list[str] = []
    state_key = _status_key(_public_gate_state(symbol_result))
    actionability_key = _public_watchlist_actionability_state_key(symbol_result)
    target_caution = actionability_key == "a_grade_actionable_target_caution" or _public_watchlist_target_caution_actionable(symbol_result)
    target_caution_confirmation_ready = _public_watchlist_target_caution_confirmation_ready(symbol_result)

    if state_key in PUBLIC_SIGNAL_NON_PUBLIC_TERMINAL_STATES:
        if not (state_key == "triggered" and target_caution and target_caution_confirmation_ready):
            reasons.append("public_block_non_public_terminal_state")
    elif state_key in PUBLIC_SIGNAL_BLOCKED_LIFECYCLE_STATES:
        reasons.append("public_block_non_actionable_state")
    elif state_key != "actionable_a_grade" and actionability_key not in PUBLIC_SIGNAL_ALLOWED_ACTIONABILITY_STATES:
        reasons.append("public_block_non_actionable_state")

    if not _public_symbol_allowed_for_public_delivery(message.symbol, context):
        reasons.append("public_block_non_crypto_symbol")

    min_score = max(_decimal_or_default(context.public_watchlist_min_score, PUBLIC_SIGNAL_MIN_SCORE), PUBLIC_SIGNAL_MIN_SCORE)
    score = _public_setup_quality_score_decimal(symbol_result)
    if score is None or score < min_score or not _public_grade_at_least(candidate.grade, context.public_watchlist_min_grade):
        reasons.append("public_block_below_quality_score")

    planned_rr = _decimal_or_none(message.planned_rr)
    if target_caution:
        if planned_rr is None or planned_rr < PUBLIC_SIGNAL_TARGET_CAUTION_MIN_RR:
            reasons.append("public_block_target_caution_rr_below_2_8")
    else:
        min_rr = max(_decimal_or_default(context.public_watchlist_min_rr, PUBLIC_SIGNAL_MIN_RR), PUBLIC_SIGNAL_MIN_RR)
        if planned_rr is None or planned_rr < min_rr:
            reasons.append("public_block_rr_below_3")

    technical_score = _technical_score_decimal(symbol_result)
    if technical_score is None or technical_score < PUBLIC_SIGNAL_MIN_TECHNICAL_SCORE:
        reasons.append("public_block_low_technical_score")

    opportunity_score = _opportunity_score_decimal(symbol_result)
    if opportunity_score is None or opportunity_score < PUBLIC_SIGNAL_MIN_OPPORTUNITY_SCORE:
        reasons.append("public_block_low_opportunity_score")

    final_failed_gate = _public_watchlist_final_failed_gate_key(symbol_result)
    if final_failed_gate != NA:
        if target_caution:
            if final_failed_gate == "target_inside_chop":
                pass
            elif final_failed_gate in {"target_integrity", "target_integrity_failed"}:
                reasons.append("public_block_target_caution_status_blocked")
            else:
                reasons.append("public_block_target_caution_not_inside_chop")
        elif "rr" in final_failed_gate:
            reasons.append("public_block_rr_below_3")
        else:
            reasons.append("public_block_non_actionable_state")

    if _public_watchlist_has_hard_public_block(symbol_result):
        reasons.append("public_block_target_caution_status_blocked" if target_caution else "public_block_non_actionable_state")

    if target_caution:
        reasons.extend(
            _public_watchlist_target_caution_blockers(
                symbol_result,
                message,
                candidate=candidate,
                score=score,
                planned_rr=planned_rr,
                technical_score=technical_score,
                opportunity_score=opportunity_score,
            )
        )

    return tuple(dict.fromkeys(reasons))

def _public_watchlist_actionability_state_key(symbol_result: ScannerSymbolResult) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    return _status_key(
        _first_non_na(
            getattr(symbol_result, "actionability_state", NA),
            getattr(lifecycle, "actionability_state", NA) if lifecycle is not None else NA,
            diagnostics.get("actionability_state"),
        )
    )


def _public_watchlist_final_failed_gate_key(symbol_result: ScannerSymbolResult) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    key = _status_key(
        _first_non_na(
            getattr(symbol_result, "final_failed_gate", NA),
            getattr(lifecycle, "final_failed_gate", NA) if lifecycle is not None else NA,
            diagnostics.get("final_failed_gate"),
        )
    )
    return key if key else NA


def _public_watchlist_has_hard_public_block(symbol_result: ScannerSymbolResult) -> bool:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    values: list[Any] = [
        getattr(symbol_result, "final_failed_gate", NA),
        getattr(lifecycle, "final_failed_gate", NA) if lifecycle is not None else NA,
        diagnostics.get("final_failed_gate"),
        diagnostics.get("active_failed_gate"),
        diagnostics.get("current_failed_gate"),
        getattr(symbol_result, "final_block_reason", NA),
        getattr(lifecycle, "final_block_reason", NA) if lifecycle is not None else NA,
        diagnostics.get("final_block_reason"),
    ]
    values.extend(_sequence_or_single(diagnostics.get("active_rejection_reasons")))
    values.extend(_sequence_or_single(diagnostics.get("current_rejection_reasons")))
    haystack = " ".join(_status_key(value) for value in values if _status_key(value))
    return any(key in haystack for key in PUBLIC_SIGNAL_HARD_FINAL_GATE_KEYS)


def _public_watchlist_target_caution_blockers(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    *,
    candidate: PublicWatchlistCandidate,
    score: Decimal | None,
    planned_rr: Decimal | None,
    technical_score: Decimal | None,
    opportunity_score: Decimal | None,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if score is None or score < PUBLIC_SIGNAL_TARGET_CAUTION_MIN_SCORE:
        blockers.append("public_block_target_caution_score_below_88")
    if planned_rr is None or planned_rr < PUBLIC_SIGNAL_TARGET_CAUTION_MIN_RR:
        blockers.append("public_block_target_caution_rr_below_2_8")
    if technical_score is None or technical_score < PUBLIC_SIGNAL_MIN_TECHNICAL_SCORE:
        blockers.append("public_block_low_technical_score")
    if opportunity_score is None or opportunity_score < PUBLIC_SIGNAL_MIN_OPPORTUNITY_SCORE:
        blockers.append("public_block_low_opportunity_score")
    if not _public_watchlist_target_caution_confirmation_ready(symbol_result):
        blockers.append("public_block_non_public_terminal_state")

    status = _public_watchlist_target_integrity_status_key(symbol_result, message)
    if status in {"blocked", "failed", "fail", "invalid", "rejected"}:
        blockers.append("public_block_target_caution_status_blocked")
    elif status != "warning":
        blockers.append("public_block_target_caution_not_inside_chop")

    if not _public_watchlist_target_caution_warning_is_exact(symbol_result):
        blockers.append("public_block_target_caution_not_inside_chop")

    blockers.extend(_public_watchlist_target_caution_failed_gate_blockers(symbol_result, candidate=candidate))
    blockers.extend(_public_watchlist_target_caution_trade_map_blockers(message))
    if _public_watchlist_target_caution_has_fatal_failure(symbol_result, message):
        blockers.append("public_block_target_caution_status_blocked")
    return tuple(dict.fromkeys(blockers))


def _public_watchlist_target_caution_confirmation_ready(symbol_result: ScannerSymbolResult) -> bool:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    confirmation_count = _first_decimal(
        getattr(symbol_result, "confirmation_count", NA),
        getattr(lifecycle, "confirmation_count", NA) if lifecycle is not None else NA,
        diagnostics.get("confirmation_count"),
    )
    required_cycles = _first_decimal(
        getattr(symbol_result, "required_confirmation_cycles", NA),
        getattr(lifecycle, "required_confirmation_cycles", NA) if lifecycle is not None else NA,
        diagnostics.get("required_confirmation_cycles"),
    )
    if confirmation_count is None and required_cycles is None:
        return True
    if confirmation_count is None:
        return False
    if required_cycles is None:
        required_cycles = Decimal("1")
    return confirmation_count >= required_cycles


def _public_watchlist_triggered_target_caution_allowed(symbol_result: ScannerSymbolResult) -> bool:
    return (
        _status_key(_public_gate_state(symbol_result)) == PUBLIC_WATCHLIST_FIRST_SEEN_TRIGGERED_STATE_KEY
        and _public_watchlist_target_caution_actionable(symbol_result)
        and _public_watchlist_target_caution_confirmation_ready(symbol_result)
    )

def _public_watchlist_target_integrity_status_key(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage | None = None,
) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    return _status_key(
        _first_non_na(
            getattr(symbol_result, "target_integrity_status", NA),
            getattr(lifecycle, "target_integrity_status", NA) if lifecycle is not None else NA,
            diagnostics.get("target_integrity_status"),
            message.target_integrity_status if message is not None else NA,
        )
    )


def _public_watchlist_target_caution_failed_gate_blockers(
    symbol_result: ScannerSymbolResult,
    *,
    candidate: PublicWatchlistCandidate,
) -> tuple[str, ...]:
    allowed = {"target_inside_chop"}
    if _public_watchlist_triggered_target_caution_allowed(symbol_result):
        allowed.add("first_seen_triggered_pre_confirmation")
    blockers: list[str] = []
    for code in _public_watchlist_failed_gate_codes(symbol_result, candidate=candidate):
        if code in allowed:
            continue
        if code in {"target_integrity", "target_integrity_failed"}:
            blockers.append("public_block_target_caution_status_blocked")
        else:
            blockers.append("public_block_target_caution_not_inside_chop")
    return tuple(dict.fromkeys(blockers))


def _public_watchlist_target_caution_trade_map_blockers(message: TelegramSignalMessage) -> tuple[str, ...]:
    blockers: list[str] = []
    if _decimal_pair_values(message.entry_low, message.entry_high) is None and _decimal_pair_text(message.watch_zone) is None:
        blockers.append("public_watchlist_missing_entry_zone")
    if _decimal_or_none(message.stop_loss) is None:
        blockers.append("public_watchlist_missing_stop")
    if _decimal_or_none(message.tp1) is None:
        blockers.append("public_watchlist_missing_tp1")
    if _decimal_or_none(message.tp2) is None:
        blockers.append("public_watchlist_missing_tp2")
    if _decimal_or_none(message.tp3) is None:
        blockers.append("public_watchlist_missing_tp3")
    return tuple(dict.fromkeys(blockers))


def _public_watchlist_target_caution_warning_is_exact(symbol_result: ScannerSymbolResult) -> bool:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    failure_key = _status_key(
        _first_non_na(
            getattr(symbol_result, "target_failure", NA),
            getattr(lifecycle, "target_failure", NA) if lifecycle is not None else NA,
            diagnostics.get("target_failure"),
            diagnostics.get("target_failure_type"),
        )
    )
    if failure_key:
        return failure_key in PUBLIC_SIGNAL_ALLOWED_TARGET_CAUTION_WARNINGS
    return _public_watchlist_target_warning_is_chop(symbol_result)


def _public_watchlist_target_caution_has_fatal_failure(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
) -> bool:
    diagnostics = _representative_diagnostics(symbol_result)
    if _target_integrity_blockers(symbol_result, TelegramAlertType.WATCHLIST, message):
        return True
    if _text(diagnostics.get("invalid_target_fields")) != NA:
        return True
    if _status_key(diagnostics.get("target_failure_severity")) in {"fatal", "fatal_target_failure", "target_failure_fatal"}:
        return True
    if _public_watchlist_target_integrity_status_key(symbol_result, message) in {"blocked", "failed", "fail", "invalid", "rejected"}:
        return True
    if _public_watchlist_final_failed_gate_key(symbol_result) in {"target_integrity", "target_integrity_failed"}:
        return True
    return _truthy_public_flag(diagnostics.get("target_integrity_failed")) or _truthy_public_flag(
        diagnostics.get("target_integrity_blocked")
    )

def _public_grade_at_least(value: Any, min_grade: Any) -> bool:
    rank = grade_rank(value)
    min_rank = grade_rank(_stricter_public_min_grade(min_grade))
    return rank is not None and min_rank is not None and rank >= min_rank


def _stricter_public_min_grade(value: Any) -> str:
    configured = normalize_grade(value)
    if grade_rank(configured) is None:
        return PUBLIC_SIGNAL_MIN_GRADE
    configured_rank = grade_rank(configured) or 0
    floor_rank = grade_rank(PUBLIC_SIGNAL_MIN_GRADE) or 0
    return configured if configured_rank >= floor_rank else PUBLIC_SIGNAL_MIN_GRADE


def _symbol_whitelist(value: Any) -> tuple[str, ...]:
    if value is None or value == NA:
        return ()
    if isinstance(value, str):
        raw_values = value.replace(";", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_values = value
    else:
        raw_values = (value,)
    return tuple(dict.fromkeys(symbol for item in raw_values if (symbol := _symbol(item)) != NA))


def _public_symbol_allowed_for_public_delivery(symbol: Any, context: TelegramEligibilityContext) -> bool:
    normalized = _symbol(symbol)
    if normalized == NA:
        return False
    if normalized in context.public_watchlist_symbol_whitelist:
        return True
    if not normalized.endswith("USDT"):
        return False
    base = normalized[:-4]
    return base not in PUBLIC_SIGNAL_NON_CRYPTO_BASE_SYMBOLS

def _public_watchlist_score_decimal(symbol_result: ScannerSymbolResult) -> Decimal | None:
    diagnostics = _representative_diagnostics(symbol_result)
    trade_idea = symbol_result.trade_idea
    setup_quality = symbol_result.setup_quality
    score_result = symbol_result.score_result
    return _first_decimal(
        getattr(setup_quality, "quality_score", NA),
        diagnostics.get("setup_quality_score"),
        diagnostics.get("quality_score"),
        getattr(score_result, "total_score", NA) if score_result is not None else NA,
        getattr(trade_idea, "confidence_score", NA) if trade_idea is not None else NA,
        diagnostics.get("opportunity_score"),
        diagnostics.get("total_score"),
    )


def _public_watchlist_status_blockers(
    symbol_result: ScannerSymbolResult,
    *,
    candidate: PublicWatchlistCandidate,
) -> tuple[str, ...]:
    blocked_status_keys = {
        "scan_error",
        "rejected_by_technical",
        "rejected_by_derivatives",
        "rejected_by_risk",
        "rejected_by_regime",
        "failed",
        "rejected",
    }
    complete_candidate_allowed_status_keys = {
        "near_miss",
        "no_setup",
        "rejected_by_scoring",
        "scanned_no_setup",
    }
    blockers: list[str] = []
    for status_key in _status_keys(symbol_result):
        if status_key in complete_candidate_allowed_status_keys:
            if not candidate.plan_complete:
                blockers.append("public_watchlist_core_status_blocked:scanned_no_setup_without_candidate")
            continue
        if status_key in blocked_status_keys:
            blockers.append(f"public_watchlist_core_status_blocked:{status_key}")
    return tuple(dict.fromkeys(blockers))


def _public_watchlist_data_health_blockers(
    symbol_result: ScannerSymbolResult,
    *,
    candidate: PublicWatchlistCandidate,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if symbol_result.timed_out or _status_key(symbol_result.timeout_status) not in {"", "none"}:
        blockers.append("public_watchlist_data_health_blocked:timeout")
    if _text(symbol_result.error_message) != NA:
        blockers.append("public_watchlist_data_health_blocked:error_message")
    data_health_flags = any(
        _sequence_or_single(getattr(symbol_result, field_name, ()))
        for field_name in (
            "missing_data",
            "unverified_data",
            "strategy_missing_data",
            "strategy_unverified_data",
            "derivatives_missing_data",
            "derivatives_unverified_data",
        )
    )
    if data_health_flags and _public_watchlist_candidate_missing_fields(candidate):
        blockers.extend(_public_watchlist_candidate_precise_missing_blockers(candidate))
    return tuple(dict.fromkeys(blockers))


def _public_watchlist_active_rejection_blockers(
    symbol_result: ScannerSymbolResult,
    *,
    candidate: PublicWatchlistCandidate,
) -> tuple[str, ...]:
    diagnostics = _representative_diagnostics(symbol_result)
    blockers: list[str] = []
    active_values: list[Any] = [
        *_diagnostic_texts(diagnostics, ACTIVE_FAILED_GATE_KEYS),
        *_diagnostic_texts(diagnostics, ACTIVE_REJECTION_REASON_KEYS),
        *_diagnostic_sequence_texts(diagnostics, ACTIVE_REJECTION_REASONS_KEYS),
    ]
    for value in active_values:
        blocker = _public_watchlist_active_rejection_blocker(value)
        if blocker != NA:
            blockers.append(blocker)

    active_invalidation = _first_non_na(*_diagnostic_texts(diagnostics, ACTIVE_INVALIDATION_REASON_KEYS))
    if _text(active_invalidation) != NA:
        blockers.append("public_watchlist_active_invalidation")

    for key in ("active_invalidation", "current_invalidation", "invalidated_now"):
        if _truthy_public_flag(diagnostics.get(key)):
            blockers.append("public_watchlist_active_invalidation")

    if candidate.lifecycle_state in {"invalidated", "expired", "cooldown", "no_longer_tracking"}:
        blockers.append("public_watchlist_active_invalidation")

    if _public_watchlist_target_caution_actionable(symbol_result):
        blockers = [
            blocker
            for blocker in blockers
            if "target_inside_chop" not in _status_key(blocker)
        ]

    return tuple(dict.fromkeys(blockers))


def _public_watchlist_active_rejection_blocker(value: Any) -> str:
    key = _status_key(value)
    if not key:
        return NA
    if any(token in key for token in ("entry_window_expired", "expired", "stale_data", "stale_or_expired")):
        return "public_watchlist_stale_or_expired"
    if any(token in key for token in ("bad_data", "low_liquidity", "wide_spread", "untradable")):
        return f"public_watchlist_active_rejection:{key}"
    for token in (
        "structural_breakdown",
        "body_acceptance_failure",
        "late_pullback",
        "target_inside_chop",
        "no_edge",
        "pullback_too_deep",
        "pullback_beyond_786",
    ):
        if token in key:
            return f"public_watchlist_active_rejection:{token}"
    return NA


def _public_watchlist_historical_rejection_only_ignored(
    symbol_result: ScannerSymbolResult,
    *,
    candidate: PublicWatchlistCandidate,
) -> bool:
    if not candidate.plan_complete:
        return False
    if _public_watchlist_active_rejection_blockers(symbol_result, candidate=candidate):
        return False
    diagnostics = _representative_diagnostics(symbol_result)
    historical_values = (
        *_diagnostic_texts(diagnostics, HISTORICAL_REJECTION_REASON_KEYS),
        *_diagnostic_sequence_texts(diagnostics, HISTORICAL_REJECTION_REASONS_KEYS),
        _text(symbol_result.rejection_reason),
        *(_text(reason) for reason in symbol_result.rejection_reasons),
    )
    return any(_text(value) != NA for value in historical_values)


def _public_watchlist_target_caution_actionable(symbol_result: ScannerSymbolResult) -> bool:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    state = _status_key(
        _first_non_na(
            getattr(symbol_result, "actionability_state", NA),
            getattr(lifecycle, "actionability_state", NA) if lifecycle is not None else NA,
            diagnostics.get("actionability_state"),
        )
    )
    severity = _status_key(
        _first_non_na(
            getattr(symbol_result, "target_failure_severity", NA),
            getattr(lifecycle, "target_failure_severity", NA) if lifecycle is not None else NA,
            diagnostics.get("target_failure_severity"),
        )
    )
    return (state == "a_grade_actionable_target_caution" or severity == "target_caution_actionable") and _public_watchlist_target_warning_is_chop(symbol_result)


def _public_watchlist_target_warning_is_chop(symbol_result: ScannerSymbolResult) -> bool:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    values = (
        getattr(symbol_result, "target_failure", NA),
        getattr(lifecycle, "target_failure", NA) if lifecycle is not None else NA,
        getattr(symbol_result, "target_warning_reason", NA),
        getattr(lifecycle, "target_warning_reason", NA) if lifecycle is not None else NA,
        diagnostics.get("target_failure"),
        diagnostics.get("target_failure_type"),
        diagnostics.get("target_warning_reason"),
        diagnostics.get("target_integrity_warning"),
        diagnostics.get("target_integrity_reason"),
    )
    key = _status_key(" ".join(_text(value) for value in values if _text(value) != NA))
    return "target_inside_chop" in key or ("tp2" in key and ("chop" in key or "range" in key))


def _public_watchlist_target_caution_grade_blocker(candidate: PublicWatchlistCandidate) -> str:
    grade_key = _status_key(candidate.grade)
    if grade_key in {"a", "a+"}:
        return NA
    return f"public_watchlist_target_caution_grade_below_a:{grade_key or 'missing'}"


def _public_watchlist_actionability_blockers(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    state = _status_key(
        _first_non_na(
            getattr(symbol_result, "actionability_state", NA),
            getattr(lifecycle, "actionability_state", NA) if lifecycle is not None else NA,
            diagnostics.get("actionability_state"),
        )
    )
    if state not in A_GRADE_PUBLIC_BLOCKED_ACTIONABILITY_STATES:
        return ()
    reason = _first_non_na(
        getattr(symbol_result, "final_block_reason", NA),
        getattr(lifecycle, "final_block_reason", NA) if lifecycle is not None else NA,
        diagnostics.get("final_block_reason"),
        getattr(symbol_result, "final_failed_gate", NA),
        getattr(lifecycle, "final_failed_gate", NA) if lifecycle is not None else NA,
        diagnostics.get("final_failed_gate"),
        state,
    )
    return (f"public_watchlist_blocked_actionability:{state}:{_text(reason)}",)



def _public_watchlist_target_integrity_blockers(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
) -> tuple[str, ...]:
    diagnostics = _representative_diagnostics(symbol_result)
    blockers = list(_target_integrity_blockers(symbol_result, TelegramAlertType.WATCHLIST, message))
    invalid_fields = _text(diagnostics.get("invalid_target_fields"))
    if invalid_fields != NA:
        blockers.append(f"target_integrity_failed:invalid_target_fields={invalid_fields}")
    if blockers:
        return tuple(dict.fromkeys(blockers))

    if _public_watchlist_target_caution_actionable(symbol_result):
        return ()

    status = _status_key(diagnostics.get("target_integrity_status"))
    if status in {"blocked", "failed", "fail", "invalid", "rejected"}:
        blockers.append(f"target_integrity_failed:status={status}")
    if _truthy_public_flag(diagnostics.get("target_integrity_failed")) or _truthy_public_flag(
        diagnostics.get("target_integrity_blocked")
    ):
        blockers.append("target_integrity_failed:diagnostic_flag")
    if any(code in {"target_integrity", "target_integrity_failed"} for code in _public_watchlist_failed_gate_codes(symbol_result)):
        blockers.append("target_integrity_failed:failed_gate")
    return tuple(dict.fromkeys(blockers))


def _public_watchlist_missing_regime_fields(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    diagnostics = _representative_diagnostics(symbol_result)
    missing: list[str] = []
    regime_state = _first_non_na(
        symbol_result.regime_state,
        diagnostics.get("regime_state"),
        _mapping_value(symbol_result.regime_diagnostics, "state"),
    )
    regime_fit = _first_non_na(
        symbol_result.regime_compatibility_label,
        diagnostics.get("regime_compatibility_label"),
        _mapping_value(symbol_result.regime_diagnostics, "compatibility_label"),
    )
    if _text(regime_state) == NA:
        missing.append("regime_state")
    if _text(regime_fit) == NA:
        missing.append("regime_compatibility_label")
    return tuple(missing)


def _truthy_public_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    key = _status_key(value)
    return key in {"1", "true", "yes", "y", "blocked", "failed", "fail", "invalid", "rejected"}


def _public_gate_state(symbol_result: ScannerSymbolResult) -> str:
    transition = symbol_result.lifecycle_transition
    if transition is not None and transition.to_state is not None:
        return transition.to_state.value
    return _lifecycle_state_text(symbol_result)


def _missing_required_fields(alert_type: TelegramAlertType, message: TelegramSignalMessage) -> tuple[str, ...]:
    required: list[tuple[str, Any]] = [
        ("signal_id", message.signal_id),
        ("symbol", message.symbol),
        ("direction", message.direction),
    ]
    if alert_type in {TelegramAlertType.SIGNAL_CONFIRMED, TelegramAlertType.LIMIT_HIT}:
        required.extend(
            [
                ("entry_low", message.entry_low),
                ("entry_high", message.entry_high),
                ("stop_loss", message.stop_loss),
                ("planned_rr", message.planned_rr),
            ]
        )
    if alert_type == TelegramAlertType.WATCHLIST:
        required.append(
            (
                "invalidation_reason",
                _first_non_na(message.watchlist_invalidation_reason, message.invalidation_reason),
            )
        )
    elif alert_type in {
        TelegramAlertType.SIGNAL_CONFIRMED,
        TelegramAlertType.LIMIT_HIT,
        TelegramAlertType.INVALIDATED,
        TelegramAlertType.EXPIRED,
        TelegramAlertType.NO_LONGER_TRACKING,
    }:
        required.append(("invalidation_reason", message.invalidation_reason))
    if alert_type in {TelegramAlertType.SIGNAL_CONFIRMED, TelegramAlertType.LIMIT_HIT}:
        required.extend((("tp1", message.tp1), ("tp2", message.tp2), ("tp3", message.tp3)))
    if alert_type == TelegramAlertType.TP1_HIT:
        required.append(("tp1", message.tp1))
    if alert_type == TelegramAlertType.TP2_HIT:
        required.append(("tp2", message.tp2))
    if alert_type == TelegramAlertType.TP3_HIT:
        required.append(("tp3", message.tp3))
    if alert_type == TelegramAlertType.SL_HIT:
        required.append(("stop_loss", message.stop_loss))
    return tuple(name for name, value in required if _text(value) == NA)


def _defensive_delivery_blockers(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
    context: TelegramEligibilityContext,
) -> tuple[str, ...]:
    if alert_type == TelegramAlertType.WATCHLIST:
        return _public_watchlist_gate_result(symbol_result, message, context).blocking_reasons

    if alert_type in TERMINAL_UPDATE_ALERT_TYPES:
        blockers = list(_terminal_transition_blockers(symbol_result, alert_type))
        missing = _missing_required_fields(alert_type, message)
        if missing:
            blockers.append(f"missing_required_fields:{','.join(missing)}")
        return tuple(dict.fromkeys(blockers))

    if alert_type != TelegramAlertType.SIGNAL_CONFIRMED:
        return ()

    blockers: list[str] = []
    rejection_context = _confirmed_rejection_context(symbol_result, message)
    quality_blockers = _public_quality_gate_blockers(symbol_result)
    blockers.extend(quality_blockers)
    if quality_blockers:
        blockers.append("confirmed_grade_below_min")
    blockers.extend(_core_status_blockers(symbol_result, current_only=True))
    blockers.extend(_failed_confirmation_core_blockers(symbol_result))

    quality_state = _setup_quality_state_key(symbol_result)
    if quality_state not in CONFIRMED_ALLOWED_QUALITY_STATE_KEYS:
        blockers.append(f"setup_quality_not_confirmed:{quality_state or 'missing'}")
        blockers.append("confirmed_grade_below_min")

    if symbol_result.trade_idea is None:
        blockers.append("trade_idea_missing")
    else:
        quality_gate = getattr(symbol_result.trade_idea, "quality_gate_result", None)
        if quality_gate is not None and getattr(quality_gate, "passed", True) is not True:
            blockers.append("quality_gate_failed")

    for reason in rejection_context.active_rejection_reasons:
        reason_key = _status_key(reason)
        blockers.append(f"confirmed_active_rejection_reason:{reason_key or 'present'}")
    if _text(rejection_context.active_failed_gate) != NA:
        blockers.append(f"confirmed_current_failed_gate:{_status_key(rejection_context.active_failed_gate)}")

    planned_rr = _decimal_or_none(message.planned_rr)
    if planned_rr is None:
        blockers.append("planned_rr_missing_or_invalid")
        blockers.append("confirmed_missing_rr")
    elif planned_rr < context.min_rr:
        blockers.append(f"planned_rr_below_min:{_text(planned_rr)}<{_text(context.min_rr)}")
        blockers.append(f"confirmed_rr_below_min:{_text(planned_rr)}<{_text(context.min_rr)}")

    opportunity_score = _opportunity_score_decimal(symbol_result)
    if context.min_score_for_idea is not None:
        if opportunity_score is None:
            blockers.append("opportunity_score_missing")
        elif opportunity_score < context.min_score_for_idea:
            blockers.append(
                f"opportunity_score_below_min:{_text(opportunity_score)}<{_text(context.min_score_for_idea)}"
            )

    technical_score = _technical_score_decimal(symbol_result)
    if technical_score is not None and technical_score < context.min_technical_score:
        blockers.append(f"technical_score_below_min:{_text(technical_score)}<{_text(context.min_technical_score)}")

    missing = _missing_required_fields(alert_type, message)
    if missing:
        blockers.append(f"missing_required_fields:{','.join(missing)}")
        if "direction" in missing:
            blockers.append("confirmed_missing_side")
        if "entry_low" in missing or "entry_high" in missing:
            blockers.append("confirmed_missing_entry_zone")
        if "stop_loss" in missing:
            blockers.append("confirmed_missing_stop")
        if "invalidation_reason" in missing:
            blockers.append("confirmed_missing_invalidation")

    if _text(message.invalidation_reason) == NA:
        blockers.append("invalidation_missing")
        blockers.append("confirmed_missing_invalidation")
    if _text(rejection_context.active_invalidation_reason) != NA:
        blockers.append("confirmed_active_invalidation")
        blockers.append("invalidation_contains_rejection_reason")

    blockers.extend(_target_integrity_blockers(symbol_result, alert_type, message))

    return tuple(dict.fromkeys(blockers))


def _target_integrity_blockers(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
) -> tuple[str, ...]:
    if alert_type not in {TelegramAlertType.WATCHLIST, TelegramAlertType.SIGNAL_CONFIRMED, TelegramAlertType.LIMIT_HIT}:
        return ()
    side = _status_key(message.direction)
    if side not in {"long", "short"}:
        return ()

    confirmed = alert_type in {TelegramAlertType.SIGNAL_CONFIRMED, TelegramAlertType.LIMIT_HIT}
    entry_reference = _entry_reference(symbol_result, message)
    stop_loss = _decimal_or_none(message.stop_loss)
    targets = (
        ("tp1", _decimal_or_none(message.tp1)),
        ("tp2", _decimal_or_none(message.tp2)),
        ("tp3", _decimal_or_none(message.tp3)),
    )
    invalid_fields: list[str] = []

    if confirmed and entry_reference is None:
        invalid_fields.append("entry_reference")
    if confirmed and stop_loss is None:
        invalid_fields.append("stop_loss")
    if confirmed:
        invalid_fields.extend(name for name, value in targets if value is None)

    if entry_reference is not None:
        if stop_loss is not None:
            if side == "long" and stop_loss >= entry_reference:
                invalid_fields.append("stop_loss")
            elif side == "short" and stop_loss <= entry_reference:
                invalid_fields.append("stop_loss")
        for name, target in targets:
            if target is None:
                continue
            if side == "long" and target <= entry_reference:
                invalid_fields.append(name)
            elif side == "short" and target >= entry_reference:
                invalid_fields.append(name)

    numeric_targets = tuple((name, value) for name, value in targets if value is not None)
    if len(numeric_targets) >= 2:
        for (_, left), (_, right) in zip(numeric_targets, numeric_targets[1:]):
            if side == "long" and left >= right:
                invalid_fields.append("tp_order")
                break
            if side == "short" and left <= right:
                invalid_fields.append("tp_order")
                break

    invalid = tuple(dict.fromkeys(invalid_fields))
    if not invalid:
        return ()
    return (f"target_integrity_failed:invalid_target_fields={','.join(invalid)}",)


def _entry_reference(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> Decimal | None:
    entry_pair = _decimal_pair_values(message.entry_low, message.entry_high)
    if entry_pair is not None:
        low, high = entry_pair
        return (low + high) / Decimal("2")

    watch_pair = _decimal_pair_text(message.watch_zone)
    if watch_pair is not None:
        low, high = watch_pair
        return (low + high) / Decimal("2")

    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    return _first_decimal(
        _field(setup, "entry"),
        _field(setup, "planned_entry"),
        _field(setup, "current_planned_entry"),
        diagnostics.get("entry"),
        diagnostics.get("entry_price"),
        diagnostics.get("planned_entry"),
        diagnostics.get("current_planned_entry"),
        diagnostics.get("limit_entry"),
        diagnostics.get("limit_price"),
        _level_field(getattr(trade_idea, "entry_zone", None), "price"),
    )


def _core_status_blockers(symbol_result: ScannerSymbolResult, *, current_only: bool = False) -> tuple[str, ...]:
    blockers: list[str] = []
    status_keys = (
        (_status_key(getattr(symbol_result.status, "value", symbol_result.status)),)
        if current_only
        else _status_keys(symbol_result)
    )
    for status_key in status_keys:
        if status_key in CONFIRMED_REJECTED_STATUS_KEYS:
            blockers.append(f"core_status_blocked:{status_key}")
    return tuple(dict.fromkeys(blockers))


def _watchlist_status_blockers(
    symbol_result: ScannerSymbolResult,
    *,
    explicit_watchlist: bool,
) -> tuple[str, ...]:
    blockers: list[str] = []
    for status_key in _status_keys(symbol_result):
        if status_key in WATCHLIST_HARD_STATUS_BLOCKERS:
            blockers.append(f"core_status_blocked:{status_key}")
        elif not explicit_watchlist and status_key in CONFIRMED_REJECTED_STATUS_KEYS:
            blockers.append(f"core_status_blocked:{status_key}")
    return tuple(dict.fromkeys(blockers))


def _watchlist_public_readiness_blockers(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    context: TelegramEligibilityContext,
    *,
    candidate: PublicWatchlistCandidate | None = None,
) -> tuple[str, ...]:
    candidate = candidate or _public_watchlist_candidate_from_symbol(symbol_result)
    missing: list[str] = []
    if _text(message.symbol) == NA:
        missing.append("symbol")
    if _status_key(message.direction) not in {"long", "short"}:
        missing.append("direction")
    if _text(message.signal_id) == NA:
        missing.append("signal_id")

    if missing:
        return (f"watchlist_not_public_ready:missing_public_fields={','.join(missing)}",)

    blockers: list[str] = []
    if not candidate.plan_complete:
        missing_public = _watchlist_missing_public_sections(message)
        if missing_public:
            blockers.append(f"watchlist_not_public_ready:missing_public_fields={','.join(missing_public)}")
    missing_trade_map: list[str] = []
    if _decimal_pair_values(message.entry_low, message.entry_high) is None:
        missing_trade_map.append("public_watchlist_missing_entry_zone")
    if _decimal_or_none(message.stop_loss) is None:
        missing_trade_map.append("public_watchlist_missing_stop")
    if _decimal_or_none(message.tp1) is None:
        missing_trade_map.append("public_watchlist_missing_tp1")
    if _text(_first_non_na(message.watchlist_invalidation_reason, message.invalidation_reason)) == NA:
        missing_trade_map.append("public_watchlist_missing_invalidation")
    if missing_trade_map:
        blockers.extend(missing_trade_map)

    if not candidate.plan_complete and _watchlist_plan_all_na(message):
        blockers.append("watchlist_missing_trackable_plan:all_plan_fields_na")
    elif not candidate.plan_complete and not _watchlist_has_tracking_anchor(symbol_result, message):
        blockers.append("watchlist_not_public_ready:no_useful_tracking_anchor")

    if not candidate.plan_complete and _watchlist_is_mostly_na(message):
        blockers.append("watchlist_not_public_ready:mostly_na_message")

    planned_rr = _decimal_or_none(message.planned_rr)
    if planned_rr is None:
        blockers.append("public_watchlist_missing_rr")
    elif planned_rr < context.min_rr:
        blockers.append(f"watchlist_not_public_ready:planned_rr_below_min:{_text(planned_rr)}<{_text(context.min_rr)}")

    return tuple(dict.fromkeys(blockers))


def _watchlist_missing_public_sections(message: TelegramSignalMessage) -> tuple[str, ...]:
    missing: list[str] = []
    context = _text(message.current_context)
    if context == NA or _looks_raw_or_generic_context(context):
        missing.append("current_context")
    needs_next = _usable_needs_next(message.needs_next)
    if not needs_next:
        missing.append("needs_next")
    invalidation = _text(_first_non_na(message.watchlist_invalidation_reason, message.invalidation_reason))
    if invalidation == NA or _looks_like_rejection_reason(invalidation):
        missing.append("invalidation")
    return tuple(missing)


def _watchlist_has_tracking_anchor(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> bool:
    if _numeric_pair_text(message.watch_zone):
        return True
    if _numeric_pair_values(message.entry_low, message.entry_high):
        return True
    if _decimal_or_none(message.stop_loss) is not None and _usable_needs_next(message.needs_next):
        return True

    diagnostics = _representative_diagnostics(symbol_result)
    trackable_level = _first_non_na(
        diagnostics.get("initial_sweep_level"),
        diagnostics.get("sweep_level"),
        diagnostics.get("swing_level"),
        diagnostics.get("ltf_swing_level"),
        diagnostics.get("price_level"),
    )
    return _decimal_or_none(trackable_level) is not None and _has_structural_tracking_context(message.current_context)


def _watchlist_plan_all_na(message: TelegramSignalMessage) -> bool:
    return (
        _decimal_or_none(message.entry_low) is None
        and _decimal_or_none(message.entry_high) is None
        and _decimal_or_none(message.stop_loss) is None
        and _decimal_or_none(message.tp1) is None
        and _decimal_or_none(message.tp2) is None
        and _decimal_or_none(message.tp3) is None
        and _decimal_or_none(message.planned_rr) is None
    )


def _watchlist_is_mostly_na(message: TelegramSignalMessage) -> bool:
    fields = (
        message.watch_zone,
        message.entry_low,
        message.entry_high,
        message.stop_loss,
        message.tp1,
        message.tp2,
        message.tp3,
        message.planned_rr,
    )
    na_count = 0
    for index, value in enumerate(fields):
        if index == 0:
            missing = _decimal_pair_text(value) is None
        else:
            missing = _decimal_or_none(value) is None
        if missing:
            na_count += 1
    return na_count >= 7


def _usable_needs_next(values: Sequence[Any]) -> tuple[str, ...]:
    usable: list[str] = []
    for value in values:
        text = _text(value)
        if text == NA:
            continue
        key = text.lower()
        if "waiting for the next lifecycle update" in key:
            continue
        if _looks_like_rejection_reason(text):
            continue
        if not _chart_only_watch_condition(text):
            continue
        usable.append(text)
    return tuple(usable)


def _watchlist_rr_warning_present(message: TelegramSignalMessage) -> bool:
    planned_rr = _decimal_or_none(message.planned_rr)
    min_rr = _decimal_or_none(message.min_rr) or DEFAULT_CONFIRMED_MIN_RR
    if planned_rr is not None and planned_rr < min_rr:
        return True
    haystack = " ".join(
        text
        for text in (
            _text(message.current_context),
            " ".join(_text(value) for value in message.needs_next if _text(value) != NA),
        )
        if text != NA
    ).lower()
    return "rr" in haystack and ("before confirmation" in haystack or "must" in haystack)


def _looks_raw_or_generic_context(value: Any) -> bool:
    text = _text(value)
    if text == NA:
        return True
    lowered = text.lower()
    return (
        text in {"Core structure is still developing.", "Setup is structurally valid and waiting for confirmation.", "N/A"}
        or "{" in text
        or "}" in text
        or "decimal(" in lowered
        or lowered in {"true", "false"}
    )


def _has_structural_tracking_context(value: Any) -> bool:
    text = _text(value).lower()
    if text == NA.lower():
        return False
    return any(token in text for token in ("sweep", "bos", "choch", "structure", "pullback", "ob/fvg"))


def _numeric_pair_values(low: Any, high: Any) -> bool:
    return _decimal_or_none(low) is not None and _decimal_or_none(high) is not None


def _decimal_pair_values(low: Any, high: Any) -> tuple[Decimal, Decimal] | None:
    low_value = _decimal_or_none(low)
    high_value = _decimal_or_none(high)
    if low_value is None or high_value is None:
        return None
    return low_value, high_value


def _numeric_pair_text(value: Any) -> bool:
    return _decimal_pair_text(value) is not None


def _decimal_pair_text(value: Any) -> tuple[Decimal, Decimal] | None:
    text = _text(value)
    if text == NA:
        return None
    parts = text.replace("\u2013", "-").replace("\u2014", "-").split("-")
    if len(parts) != 2:
        return None
    low = _decimal_or_none(parts[0].strip())
    high = _decimal_or_none(parts[1].strip())
    if low is None or high is None:
        return None
    return low, high


def _status_keys(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    values: list[Any] = [getattr(symbol_result.status, "value", symbol_result.status)]
    values.extend(getattr(status, "value", status) for status in symbol_result.status_history)
    return tuple(dict.fromkeys(_status_key(value) for value in values if _status_key(value)))


def _setup_quality_state_key(symbol_result: ScannerSymbolResult) -> str:
    quality_state = getattr(symbol_result.setup_quality, "quality_state", NA)
    return _status_key(getattr(quality_state, "value", quality_state))


def _public_quality_gate_blockers(symbol_result: ScannerSymbolResult, *, min_grade: str = MIN_PUBLIC_SIGNAL_GRADE) -> tuple[str, ...]:
    diagnostics = _representative_diagnostics(symbol_result)
    trade_idea = symbol_result.trade_idea
    setup_quality = symbol_result.setup_quality
    decision = public_quality_decision(
        grade_candidates=(
            getattr(getattr(setup_quality, "quality_grade", None), "value", NA),
            getattr(trade_idea, "grade", NA) if trade_idea is not None else NA,
            diagnostics.get("quality_grade"),
            diagnostics.get("opportunity_grade"),
            diagnostics.get("grade"),
            diagnostics.get("trust_grade"),
        ),
        score_candidates=(
            getattr(setup_quality, "quality_score", NA),
            getattr(trade_idea, "confidence_score", NA) if trade_idea is not None else NA,
            diagnostics.get("setup_quality_score"),
            diagnostics.get("quality_score"),
            diagnostics.get("opportunity_score"),
            diagnostics.get("confidence_score"),
            diagnostics.get("trust_percentage"),
            diagnostics.get("readiness_score"),
        ),
        min_grade=min_grade,
    )
    if decision.passed:
        return ()
    grade = decision.grade if _text(decision.grade) != NA else NA
    source = decision.source if _text(decision.source) != NA else NA
    return (f"{decision.reason}:grade={grade}:min={min_grade}:source={source}",)


def _public_watchlist_quality_gate_blockers(
    candidate: PublicWatchlistCandidate,
    *,
    min_grade: str = MIN_PUBLIC_SIGNAL_GRADE,
) -> tuple[str, ...]:
    if _text(candidate.grade) == NA:
        return ("public_watchlist_missing_grade",)
    decision = public_quality_decision(
        grade_candidates=(candidate.grade,),
        score_candidates=(),
        min_grade=min_grade,
    )
    if decision.passed:
        return ()
    grade = decision.grade if _text(decision.grade) != NA else NA
    source = decision.source if _text(decision.source) != NA else NA
    return (f"public_watchlist_below_min_grade:{decision.reason}:grade={grade}:min={min_grade}:source={source}",)


def _watchlist_candidate_expiry_decision(symbol_result: ScannerSymbolResult):
    transition = symbol_result.lifecycle_transition
    timestamp = transition.event.timestamp if transition is not None and transition.event is not None else NA
    lifecycle = symbol_result.lifecycle_state
    # For a new public WATCHLIST candidate, the transition event timestamp is the safest
    # promoted-at anchor. Lifecycle first_seen_at can predate public promotion and would
    # expire internal research rows before they were ever shown publicly.
    return watchlist_expiry_decision(
        timestamp_candidates=(timestamp,),
        state_candidates=(
            transition.to_state if transition is not None else NA,
            getattr(lifecycle, "current_state", NA) if lifecycle is not None else NA,
            getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA,
        ),
    )


def _explicit_watchlist_candidate(symbol_result: ScannerSymbolResult) -> bool:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    quality_state = _setup_quality_state_key(symbol_result)
    if quality_state == "watchlist_near_miss":
        return True

    action_values = (
        getattr(symbol_result.setup_quality, "action_label", NA),
        getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA,
        _field(intelligence, "action_label"),
        _field(intelligence, "watchlist_status"),
        diagnostics.get("action_label"),
        diagnostics.get("watchlist_status"),
    )
    if any(_status_key(value) in WATCHLIST_ACTION_KEYS for value in action_values):
        return True

    pullback_payload = diagnostics.get("pullback_intelligence")
    lifecycle_action = _first_non_na(
        _field(_field(pullback_payload, "projection"), "lifecycle_action"),
        diagnostics.get("lifecycle_action"),
    )
    return _status_key(lifecycle_action) in WATCHLIST_LIFECYCLE_ACTION_KEYS


def _persist_blocked_decision(decision: TelegramAlertDecision) -> bool:
    if decision.alert_type in TERMINAL_UPDATE_ALERT_TYPES:
        return decision.reason in TERMINAL_IDENTITY_BLOCK_REASONS or decision.reason.startswith("blocked:")
    if decision.alert_type in TP_SL_ALERT_TYPES:
        return decision.reason.startswith("blocked:")
    if decision.alert_type == TelegramAlertType.LIMIT_HIT:
        return decision.reason.startswith("blocked:")
    return decision.alert_type in {TelegramAlertType.WATCHLIST, TelegramAlertType.SIGNAL_CONFIRMED} and decision.reason.startswith(
        "blocked:"
    )


def _invalid_target_fields_from_reason(reason: str) -> str:
    marker = "invalid_target_fields="
    text = _text(reason)
    if marker not in text:
        return NA
    fields = text.split(marker, 1)[1].split(";", 1)[0].strip()
    return fields if fields else NA


def _persist_blocked_attempt(
    repository: SQLiteTelegramAlertAttemptRepository,
    symbol_result: ScannerSymbolResult,
    *,
    decision: TelegramAlertDecision,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> TelegramLifecycleDelivery:
    assert decision.alert_type is not None
    assert decision.message is not None
    signal_id = _signal_id_for_alert(symbol_result, decision.alert_type, decision.message)
    blocked_alert_type = _blocked_alert_type(decision.alert_type, decision.reason)
    transition = decision.lifecycle_transition
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
    seen_at = now_utc_iso()
    message_hash = hashlib.sha256(
        f"{signal_id}|{decision.alert_type.value}|{decision.reason}".encode("utf-8")
    ).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=signal_id,
        symbol=symbol_result.symbol,
        direction=decision.message.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=blocked_alert_type,
        lifecycle_state=_lifecycle_state_text(symbol_result),
        sent_at=None,
        attempted_at=seen_at,
        telegram_status="blocked",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=decision.alert_type.value,
        setup_quality_score=_quality_score(symbol_result),
        rr_planned=_text(decision.message.planned_rr),
        min_rr=_text(_min_rr_for_alert(decision.alert_type, eligibility_context)),
        opportunity_score=_opportunity_score_text(symbol_result),
        min_score_for_idea=_text(eligibility_context.min_score_for_idea),
        technical_score=_technical_score_text(symbol_result),
        price_level=_price_level_for_alert(decision.alert_type, decision.message),
        **_message_level_metadata(decision.message),
        **_public_watchlist_record_plan_fields(symbol_result, decision.alert_type, decision.message),
        blocked_reason=decision.reason,
        invalid_target_fields=_invalid_target_fields_from_reason(decision.reason),
        error_message=decision.reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=decision.reason,
    )
    inserted = repository.insert_attempt(record)
    if not inserted:
        compacted = repository.compact_repeated_attempt(record)
        status = "blocked_repeat" if compacted else "duplicate"
        detail = (
            "Repeated blocked Telegram alert attempt compacted."
            if compacted
            else "Duplicate blocked Telegram alert attempt prevented."
        )
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=decision.alert_type.value,
            status=status,
            detail=detail,
            message_hash=message_hash,
            error_message=decision.reason,
        )
    return TelegramLifecycleDelivery(
        symbol=symbol_result.symbol,
        signal_id=signal_id,
        alert_type=decision.alert_type.value,
        status="blocked",
        detail=_blocked_delivery_detail(decision.alert_type),
        message_hash=message_hash,
        error_message=decision.reason,
    )


def _persist_skipped_public_watchlist_attempt(
    repository: SQLiteTelegramAlertAttemptRepository,
    symbol_result: ScannerSymbolResult,
    *,
    decision: TelegramAlertDecision,
    reason: str,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> TelegramLifecycleDelivery:
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert decision.message is not None
    signal_id = _signal_id_for_alert(symbol_result, decision.alert_type, decision.message)
    skipped_alert_type = _blocked_alert_type(decision.alert_type, reason)
    transition = decision.lifecycle_transition
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
    seen_at = now_utc_iso()
    message_hash = hashlib.sha256(f"{signal_id}|{decision.alert_type.value}|{reason}".encode("utf-8")).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=signal_id,
        symbol=symbol_result.symbol,
        direction=decision.message.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=skipped_alert_type,
        lifecycle_state=_lifecycle_state_text(symbol_result),
        sent_at=None,
        attempted_at=seen_at,
        telegram_status="skipped",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=decision.alert_type.value,
        setup_quality_score=_quality_score(symbol_result),
        rr_planned=_text(decision.message.planned_rr),
        min_rr=_text(_min_rr_for_alert(decision.alert_type, eligibility_context)),
        opportunity_score=_opportunity_score_text(symbol_result),
        min_score_for_idea=_text(eligibility_context.min_score_for_idea),
        technical_score=_technical_score_text(symbol_result),
        price_level=_price_level_for_alert(decision.alert_type, decision.message),
        **_message_level_metadata(decision.message),
        **_public_watchlist_record_plan_fields(symbol_result, decision.alert_type, decision.message),
        blocked_reason=reason,
        invalid_target_fields=NA,
        error_message=reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=reason,
    )
    inserted = repository.insert_attempt(record)
    if not inserted:
        compacted = repository.compact_repeated_attempt(record)
        status = "skipped" if compacted else "duplicate"
        detail = (
            "Repeated skipped public watchlist attempt compacted."
            if compacted
            else "Duplicate skipped public watchlist attempt prevented."
        )
    else:
        status = "skipped"
        detail = "Public watchlist alert skipped by cooldown or scan throttle."
    return TelegramLifecycleDelivery(
        symbol=symbol_result.symbol,
        signal_id=signal_id,
        alert_type=decision.alert_type.value,
        status=status,
        detail=detail,
        message_hash=message_hash,
        error_message=reason,
    )


def _persist_sent_watchlist_reconciliation_block(
    repository: SQLiteTelegramAlertAttemptRepository,
    prior_alert: TelegramAlertAttemptRecord,
    *,
    reason: str,
    scan_run_id: str | None,
) -> TelegramLifecycleDelivery:
    seen_at = now_utc_iso()
    blocked_alert_type = _blocked_alert_type(TelegramAlertType.NO_LONGER_TRACKING, reason)
    message_hash = hashlib.sha256(
        f"{prior_alert.signal_id}|{SENT_WATCHLIST_RECONCILIATION_ATTEMPT}|{reason}".encode("utf-8")
    ).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=NA,
        new_state=NA,
        alert_type=blocked_alert_type,
        lifecycle_state=NA,
        sent_at=None,
        attempted_at=seen_at,
        telegram_status="blocked",
        message_hash=message_hash,
        scan_run_id=scan_run_id,
        attempted_alert_type=SENT_WATCHLIST_RECONCILIATION_ATTEMPT,
        setup_quality_score=NA,
        rr_planned=NA,
        min_rr=NA,
        opportunity_score=NA,
        min_score_for_idea=NA,
        technical_score=NA,
        price_level=NA,
        blocked_reason=reason,
        invalid_target_fields=NA,
        error_message=reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id,
        last_error_message=reason,
    )
    inserted = repository.insert_attempt(record)
    if not inserted:
        compacted = repository.compact_repeated_attempt(record)
        status = "blocked_repeat" if compacted else "duplicate"
        detail = (
            "Repeated sent-watchlist reconciliation block compacted."
            if compacted
            else "Duplicate sent-watchlist reconciliation block prevented."
        )
        return TelegramLifecycleDelivery(
            symbol=prior_alert.symbol,
            signal_id=prior_alert.signal_id,
            alert_type=SENT_WATCHLIST_RECONCILIATION_ATTEMPT,
            status=status,
            detail=detail,
            message_hash=message_hash,
            error_message=reason,
        )
    return TelegramLifecycleDelivery(
        symbol=prior_alert.symbol,
        signal_id=prior_alert.signal_id,
        alert_type=SENT_WATCHLIST_RECONCILIATION_ATTEMPT,
        status="blocked",
        detail="Sent watchlist reconciliation blocked by lifecycle identity guard.",
        message_hash=message_hash,
        error_message=reason,
    )


def _persist_watchlist_outcome_audit(
    repository: SQLiteTelegramAlertAttemptRepository,
    prior_alert: TelegramAlertAttemptRecord,
    *,
    reason: str,
    scan_run_id: str | None,
    symbol_result: ScannerSymbolResult | None = None,
    message: TelegramSignalMessage | None = None,
    price_level: str = NA,
) -> None:
    seen_at = now_utc_iso()
    alert_type = _watchlist_outcome_audit_alert_type(reason)
    transition = symbol_result.lifecycle_transition if symbol_result is not None else None
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result) if symbol_result else NA
    message_hash = hashlib.sha256(
        f"{prior_alert.signal_id}|{WATCHLIST_OUTCOME_TRACKING_ATTEMPT}|{reason}".encode("utf-8")
    ).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=alert_type,
        lifecycle_state=_lifecycle_state_text(symbol_result) if symbol_result is not None else NA,
        sent_at=None,
        attempted_at=seen_at,
        telegram_status="skipped",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=WATCHLIST_OUTCOME_TRACKING_ATTEMPT,
        setup_quality_score=_quality_score(symbol_result) if symbol_result is not None else NA,
        rr_planned=_text(message.planned_rr) if message is not None else NA,
        min_rr=_text(message.min_rr) if message is not None else NA,
        opportunity_score=_opportunity_score_text(symbol_result) if symbol_result is not None else NA,
        min_score_for_idea=NA,
        technical_score=_technical_score_text(symbol_result) if symbol_result is not None else NA,
        price_level=price_level,
        **_message_level_metadata(message),
        blocked_reason=reason,
        invalid_target_fields=NA,
        error_message=reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=reason,
    )
    if not repository.insert_attempt(record):
        repository.compact_repeated_attempt(record)


def _persist_suppressed_watchlist_terminal_update(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    prior_alert: TelegramAlertAttemptRecord,
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage | None,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> TelegramLifecycleDelivery:
    seen_at = now_utc_iso()
    reason = "public_watchlist_follow_up_updates_disabled"
    skipped_alert_type = _watchlist_terminal_suppression_alert_type(alert_type)
    transition = symbol_result.lifecycle_transition
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
    message_hash = hashlib.sha256(
        f"{prior_alert.signal_id}|{WATCHLIST_TERMINAL_SUPPRESSION_ATTEMPT}|{alert_type.value}".encode("utf-8")
    ).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=skipped_alert_type,
        lifecycle_state=_lifecycle_state_text(symbol_result),
        sent_at=None,
        attempted_at=seen_at,
        telegram_status="skipped",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=alert_type.value,
        setup_quality_score=_quality_score(symbol_result),
        rr_planned=_text(message.planned_rr) if message is not None else NA,
        min_rr=_text(eligibility_context.min_rr),
        opportunity_score=_opportunity_score_text(symbol_result),
        min_score_for_idea=_text(eligibility_context.min_score_for_idea),
        technical_score=_technical_score_text(symbol_result),
        price_level=_price_level_for_alert(alert_type, message) if message is not None else NA,
        **_message_level_metadata(message),
        blocked_reason=reason,
        invalid_target_fields=NA,
        error_message=reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=reason,
    )
    inserted = repository.insert_attempt(record)
    status = "skipped"
    detail = "Public watchlist follow-up update suppressed."
    if not inserted:
        compacted = repository.compact_repeated_attempt(record)
        status = "blocked_repeat" if compacted else "duplicate"
        detail = (
            "Repeated suppressed watchlist follow-up update compacted."
            if compacted
            else "Duplicate suppressed watchlist follow-up update prevented."
        )
    return TelegramLifecycleDelivery(
        symbol=prior_alert.symbol,
        signal_id=prior_alert.signal_id,
        alert_type=alert_type.value,
        status=status,
        detail=detail,
        message_hash=message_hash,
        error_message=reason,
    )


def _prior_watchlist_expiry_decision(
    repository: SQLiteTelegramAlertAttemptRepository,
    prior_alert: TelegramAlertAttemptRecord,
):
    attempts = repository.list_attempts(signal_id=prior_alert.signal_id)
    state_candidates = (
        prior_alert.lifecycle_state,
        prior_alert.new_state,
        prior_alert.alert_type,
        *(attempt.lifecycle_state for attempt in attempts),
        *(attempt.new_state for attempt in attempts),
        *(attempt.alert_type for attempt in attempts),
    )
    # Public watchlist TTL starts from telegram_alert_attempts.first_seen_at because
    # that column records the first public WATCHLIST display/send for a signal. If
    # older databases lack it, sent_at is the deterministic public-send fallback.
    return watchlist_expiry_decision(
        timestamp_candidates=(prior_alert.first_seen_at, prior_alert.sent_at),
        state_candidates=state_candidates,
        now=_parse_iso_datetime(now_utc_iso()),
    )


def _persist_watchlist_expiry_audit(
    repository: SQLiteTelegramAlertAttemptRepository,
    prior_alert: TelegramAlertAttemptRecord,
    *,
    reason: str,
    scan_run_id: str | None,
) -> TelegramLifecycleDelivery:
    seen_at = now_utc_iso()
    alert_type = _watchlist_expiry_audit_alert_type(reason)
    message_hash = hashlib.sha256(
        f"{prior_alert.signal_id}|{WATCHLIST_EXPIRY_ATTEMPT}|{reason}".encode("utf-8")
    ).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=prior_alert.previous_state,
        new_state=prior_alert.new_state,
        alert_type=alert_type,
        lifecycle_state=prior_alert.lifecycle_state,
        sent_at=None,
        attempted_at=seen_at,
        telegram_status="skipped",
        message_hash=message_hash,
        scan_run_id=scan_run_id,
        attempted_alert_type=WATCHLIST_EXPIRY_ATTEMPT,
        setup_quality_score=prior_alert.setup_quality_score,
        rr_planned=prior_alert.rr_planned,
        min_rr=prior_alert.min_rr,
        opportunity_score=prior_alert.opportunity_score,
        min_score_for_idea=prior_alert.min_score_for_idea,
        technical_score=prior_alert.technical_score,
        price_level=prior_alert.price_level,
        entry_low=prior_alert.entry_low,
        entry_high=prior_alert.entry_high,
        stop_loss=prior_alert.stop_loss,
        tp1=prior_alert.tp1,
        tp2=prior_alert.tp2,
        tp3=prior_alert.tp3,
        blocked_reason=reason,
        invalid_target_fields=NA,
        error_message=reason,
        first_seen_at=_first_non_na(prior_alert.first_seen_at, prior_alert.sent_at),
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id,
        last_error_message=reason,
    )
    inserted = repository.insert_attempt(record)
    status = "skipped"
    detail = "Public watchlist expired after the 48-hour watch TTL."
    if not inserted:
        compacted = repository.compact_repeated_attempt(record)
        status = "blocked_repeat" if compacted else "duplicate"
        detail = (
            "Repeated public watchlist expiry audit compacted."
            if compacted
            else "Duplicate public watchlist expiry audit prevented."
        )
    return TelegramLifecycleDelivery(
        symbol=prior_alert.symbol,
        signal_id=prior_alert.signal_id,
        alert_type=WATCHLIST_EXPIRY_ATTEMPT,
        status=status,
        detail=detail,
        message_hash=message_hash,
        error_message=reason,
    )


def _watchlist_outcome_for_current_result(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    prior_alert: TelegramAlertAttemptRecord,
    current_result: ScannerSymbolResult,
    eligibility_context: TelegramEligibilityContext,
    scan_run_id: str | None,
) -> tuple[TelegramAlertType, TelegramSignalMessage] | TelegramLifecycleDelivery | None:
    if current_result.lifecycle_outcome_progress is not None:
        return None
    if prior_alert.alert_type != TelegramAlertType.WATCHLIST.value or prior_alert.telegram_status != "sent":
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_no_prior_public_watchlist",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
        )
        return None
    if repository.has_sent_terminal_outcome(signal_id=prior_alert.signal_id):
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_already_closed",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
        )
        return None
    terminal_state_reason = _watchlist_outcome_terminal_state_reason(current_result)
    if terminal_state_reason is not None:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason=terminal_state_reason,
            scan_run_id=scan_run_id,
            symbol_result=current_result,
        )
        return None

    prior_signal_alert = repository.get_prior_public_signal_alert(signal_ids=(prior_alert.signal_id,))
    message = _message_with_prior_public_plan(
        replace(
            telegram_signal_message_from_symbol(current_result),
            min_rr=eligibility_context.min_rr,
            watchlist_outcome=True,
        ),
        prior_signal_alert or prior_alert,
    )
    side = _status_key(message.direction)
    limit_zone = _limit_zone_values(message)
    if side not in {"long", "short"} or limit_zone is None:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_missing_entry",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
        )
        return None

    candle = _watchlist_candle_snapshot(current_result)
    if candle is None:
        return None

    stop_loss = _valid_watchlist_stop(message, limit_zone)
    if stop_loss is None:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_missing_stop",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
        )

    targets, target_tracking_unresolved = _valid_watchlist_targets(message, limit_zone)
    if target_tracking_unresolved:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_missing_targets",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
        )

    if not repository.has_attempt(signal_id=prior_alert.signal_id, alert_type=TelegramAlertType.LIMIT_HIT):
        if not _candle_touches_zone(candle, limit_zone):
            _persist_watchlist_outcome_audit(
                repository,
                prior_alert,
                reason="outcome_tracking_not_limit_hit_yet",
                scan_run_id=scan_run_id,
                symbol_result=current_result,
                message=message,
                price_level=_price_level_for_alert(TelegramAlertType.LIMIT_HIT, message),
            )
            return None
        if prior_signal_alert is None:
            _persist_watchlist_outcome_audit(
                repository,
                prior_alert,
                reason="outcome_tracking_limit_hit_requires_prior_public_signal",
                scan_run_id=scan_run_id,
                symbol_result=current_result,
                message=message,
                price_level=_price_level_for_alert(TelegramAlertType.LIMIT_HIT, message),
            )
            return None
        public_gate = _public_signal_gate_result(
            current_result,
            TelegramAlertType.LIMIT_HIT,
            message,
            prior_public_alert=prior_signal_alert,
        )
        if not public_gate.allowed:
            _persist_watchlist_outcome_audit(
                repository,
                prior_alert,
                reason="outcome_tracking_public_gate_blocked:" + ",".join(public_gate.reason_codes),
                scan_run_id=scan_run_id,
                symbol_result=current_result,
                message=message,
                price_level=_price_level_for_alert(TelegramAlertType.LIMIT_HIT, message),
            )
            return None
        tracked_targets = () if target_tracking_unresolved else targets
        if _same_candle_touches_post_limit_outcome(candle, side=side, stop_loss=stop_loss, targets=tracked_targets):
            _persist_watchlist_outcome_audit(
                repository,
                prior_alert,
                reason="outcome_tracking_same_candle_ambiguous",
                scan_run_id=scan_run_id,
                symbol_result=current_result,
                message=message,
                price_level=_price_level_for_alert(TelegramAlertType.LIMIT_HIT, message),
            )
        return TelegramAlertType.LIMIT_HIT, message

    if repository.has_attempt(signal_id=prior_alert.signal_id, alert_type=TelegramAlertType.SL_HIT) or repository.has_attempt(
        signal_id=prior_alert.signal_id,
        alert_type=TelegramAlertType.TP3_HIT,
    ):
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_already_closed",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
        )
        return None

    tp_sl_state_reason = _watchlist_tp_sl_state_block_reason(current_result)
    if tp_sl_state_reason is not None:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason=tp_sl_state_reason,
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
        )
        return None

    tracked_targets = () if target_tracking_unresolved else targets
    legacy_sl_hit = stop_loss is not None and _stop_touched(candle, side=side, stop_loss=stop_loss)
    legacy_next_tp = _next_touched_watchlist_tp(
        repository,
        prior_alert.signal_id,
        candle,
        side=side,
        targets=tracked_targets,
    )
    live_price, live_price_reason = _live_price_snapshot(current_result)
    if live_price is None:
        legacy_alert_type = TelegramAlertType.SL_HIT if legacy_sl_hit else legacy_next_tp
        if legacy_alert_type is None:
            return None
        return _persist_blocked_watchlist_tp_sl_attempt(
            repository,
            prior_alert,
            symbol_result=current_result,
            alert_type=legacy_alert_type,
            message=message,
            reason=f"blocked:{live_price_reason}",
            scan_run_id=scan_run_id,
        )

    sl_hit = stop_loss is not None and _tp_sl_price_condition(
        live_price.price,
        TelegramAlertType.SL_HIT,
        side=side,
        level=stop_loss,
    )
    next_tp = _next_touched_watchlist_tp_from_price(
        repository,
        prior_alert.signal_id,
        live_price.price,
        side=side,
        targets=tracked_targets,
    )
    if sl_hit and next_tp is not None:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_same_candle_ambiguous",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
            price_level=_price_level_for_alert(next_tp, message),
        )
        return None
    legacy_alert_type = TelegramAlertType.SL_HIT if legacy_sl_hit else legacy_next_tp
    if not sl_hit and next_tp is None and legacy_alert_type is not None:
        blockers = _watchlist_tp_sl_blockers(
            repository,
            prior_alert=prior_alert,
            symbol_result=current_result,
            alert_type=legacy_alert_type,
            message=message,
            live_price=live_price,
            require_limit_hit=True,
        )
        reason = f"blocked:{'; '.join(blockers or ('tp_sl_price_condition_false',))}"
        return _persist_blocked_watchlist_tp_sl_attempt(
            repository,
            prior_alert,
            symbol_result=current_result,
            alert_type=legacy_alert_type,
            message=message,
            reason=reason,
            scan_run_id=scan_run_id,
        )
    if sl_hit:
        blockers = _watchlist_tp_sl_blockers(
            repository,
            prior_alert=prior_alert,
            symbol_result=current_result,
            alert_type=TelegramAlertType.SL_HIT,
            message=message,
            live_price=live_price,
            require_limit_hit=True,
        )
        if blockers:
            return _persist_blocked_watchlist_tp_sl_attempt(
                repository,
                prior_alert,
                symbol_result=current_result,
                alert_type=TelegramAlertType.SL_HIT,
                message=message,
                reason=f"blocked:{'; '.join(blockers)}",
                scan_run_id=scan_run_id,
            )
        return TelegramAlertType.SL_HIT, replace(
            message,
            price_level=live_price.price,
        )
    if next_tp is not None:
        blockers = _watchlist_tp_sl_blockers(
            repository,
            prior_alert=prior_alert,
            symbol_result=current_result,
            alert_type=next_tp,
            message=message,
            live_price=live_price,
            require_limit_hit=True,
        )
        if blockers:
            return _persist_blocked_watchlist_tp_sl_attempt(
                repository,
                prior_alert,
                symbol_result=current_result,
                alert_type=next_tp,
                message=message,
                reason=f"blocked:{'; '.join(blockers)}",
                scan_run_id=scan_run_id,
            )
        return next_tp, replace(
            message,
            price_level=live_price.price,
        )
    return None


def _watchlist_outcome_terminal_state_reason(symbol_result: ScannerSymbolResult) -> str | None:
    state_key = _status_key(_lifecycle_state_text(symbol_result))
    if state_key in {"invalidated", "cooldown", "cooled_down", "expired", "no_longer_tracking", "rejected", "archived"}:
        return f"outcome_tracking_terminal_lifecycle_state:{state_key}"
    return None


def _watchlist_tp_sl_state_block_reason(symbol_result: ScannerSymbolResult) -> str | None:
    blocked_keys = {"watch", "watchlist", "watchlisted", "stalking", "triggered", "rejected", "invalidated", "cooldown"}
    candidates = [_lifecycle_state_text(symbol_result)]
    transition = symbol_result.lifecycle_transition
    if transition is not None:
        candidates.append(transition.to_state.value if transition.to_state is not None else NA)
    for value in candidates:
        key = _status_key(value)
        if key in blocked_keys:
            return f"outcome_tracking_tp_sl_lifecycle_state_blocked:{key}"
    return None


def _tp_sl_delivery_blockers(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    symbol_result: ScannerSymbolResult,
    signal_id: str,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
) -> tuple[str, ...]:
    prior_public_alert = repository.get_prior_public_alert(signal_ids=(signal_id,))
    return _tp_sl_common_blockers(
        repository,
        symbol_result=symbol_result,
        signal_id=signal_id,
        alert_type=alert_type,
        message=message,
        prior_public_alert=prior_public_alert,
        require_limit_hit=False,
    )


def _watchlist_tp_sl_blockers(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    prior_alert: TelegramAlertAttemptRecord,
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
    live_price: WatchlistLivePriceSnapshot | None = None,
    require_limit_hit: bool,
) -> tuple[str, ...]:
    return _tp_sl_common_blockers(
        repository,
        symbol_result=symbol_result,
        signal_id=prior_alert.signal_id,
        alert_type=alert_type,
        message=message,
        prior_public_alert=prior_alert,
        live_price=live_price,
        require_limit_hit=require_limit_hit,
    )


def _tp_sl_common_blockers(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    symbol_result: ScannerSymbolResult,
    signal_id: str,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
    prior_public_alert: TelegramAlertAttemptRecord | None,
    live_price: WatchlistLivePriceSnapshot | None = None,
    require_limit_hit: bool,
) -> tuple[str, ...]:
    blockers: list[str] = []
    expected_symbol = _symbol(message.symbol)
    result_symbol = _symbol(symbol_result.symbol)
    prior_symbol = _symbol(prior_public_alert.symbol) if prior_public_alert is not None else NA
    if expected_symbol == NA:
        blockers.append("tp_sl_missing_symbol")
    if expected_symbol != NA and result_symbol != NA and result_symbol != expected_symbol:
        blockers.append("tp_sl_symbol_mismatch")
    if expected_symbol != NA and prior_symbol != NA and prior_symbol != expected_symbol:
        blockers.append("tp_sl_symbol_mismatch")

    blockers.extend(
        _tp_sl_tracking_state_blockers(
            repository,
            signal_id=signal_id,
            symbol_result=symbol_result,
            prior_public_alert=prior_public_alert,
            require_limit_hit=require_limit_hit,
        )
    )

    snapshot = live_price
    live_price_reason = NA
    if snapshot is None:
        snapshot, live_price_reason = _live_price_snapshot(symbol_result)
    if snapshot is None:
        blockers.append(live_price_reason)
        return tuple(dict.fromkeys(blocker for blocker in blockers if blocker != NA))
    if expected_symbol != NA and _symbol(snapshot.symbol) != expected_symbol:
        blockers.append("tp_sl_symbol_mismatch")

    side = _status_key(message.direction)
    if side not in {"long", "short"}:
        blockers.append("tp_sl_missing_direction")
    level = _tp_sl_level(alert_type, message)
    if level is None:
        blockers.append("tp_sl_missing_level")
    if side in {"long", "short"} and level is not None:
        blockers.extend(_tp_sl_price_blockers(snapshot.price, alert_type=alert_type, side=side, level=level))
    return tuple(dict.fromkeys(blocker for blocker in blockers if blocker != NA))


def _tp_sl_tracking_state_blockers(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    signal_id: str,
    symbol_result: ScannerSymbolResult,
    prior_public_alert: TelegramAlertAttemptRecord | None,
    require_limit_hit: bool,
) -> tuple[str, ...]:
    has_limit_hit = repository.has_attempt(signal_id=signal_id, alert_type=TelegramAlertType.LIMIT_HIT)
    state_keys = set(_tp_sl_lifecycle_state_keys(symbol_result))
    active_evidence = has_limit_hit or bool(state_keys & TP_SL_TRACKING_ACTIVE_STATE_KEYS)
    entry_touched_evidence = has_limit_hit or bool(state_keys & TP_SL_ENTRY_TOUCHED_STATE_KEYS)
    blockers: list[str] = []
    if require_limit_hit and not has_limit_hit:
        blockers.append("tp_sl_before_entry_zone_touched")
    if not active_evidence:
        blockers.append("tp_sl_not_active")
    if not entry_touched_evidence or (
        prior_public_alert is not None
        and prior_public_alert.alert_type == TelegramAlertType.WATCHLIST.value
        and not has_limit_hit
    ):
        blockers.append("tp_sl_before_entry_zone_touched")
    return tuple(dict.fromkeys(blockers))


def _tp_sl_lifecycle_state_keys(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    values: list[Any] = [_lifecycle_state_text(symbol_result)]
    transition = symbol_result.lifecycle_transition
    if transition is not None:
        values.extend(
            (
                transition.from_state.value if transition.from_state is not None else NA,
                transition.to_state.value if transition.to_state is not None else NA,
            )
        )
    previous = _previous_transition_state(symbol_result)
    if previous is not None:
        values.append(previous.value)
    return tuple(dict.fromkeys(_status_key(value) for value in values if _status_key(value)))


def _tp_sl_price_blockers(
    current_price: Decimal,
    *,
    alert_type: TelegramAlertType,
    side: str,
    level: Decimal,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not _tp_sl_price_condition(current_price, alert_type, side=side, level=level):
        blockers.append("tp_sl_price_condition_false")
    if _tp_sl_price_distance_unrealistic(current_price, level):
        blockers.append("tp_sl_price_distance_unrealistic")
    return tuple(blockers)


def _tp_sl_price_condition(
    current_price: Decimal,
    alert_type: TelegramAlertType,
    *,
    side: str,
    level: Decimal,
) -> bool:
    if alert_type in {TelegramAlertType.TP1_HIT, TelegramAlertType.TP2_HIT, TelegramAlertType.TP3_HIT}:
        if side == "long":
            return current_price >= level
        if side == "short":
            return current_price <= level
    if alert_type == TelegramAlertType.SL_HIT:
        if side == "long":
            return current_price <= level
        if side == "short":
            return current_price >= level
    return False


def _tp_sl_price_distance_unrealistic(current_price: Decimal, level: Decimal) -> bool:
    if level == 0:
        return True
    return abs(current_price - level) / abs(level) > MAX_TP_SL_EVENT_DISTANCE_PCT


def _tp_sl_level(alert_type: TelegramAlertType, message: TelegramSignalMessage) -> Decimal | None:
    if alert_type == TelegramAlertType.TP1_HIT:
        return _decimal_or_none(message.tp1)
    if alert_type == TelegramAlertType.TP2_HIT:
        return _decimal_or_none(message.tp2)
    if alert_type == TelegramAlertType.TP3_HIT:
        return _decimal_or_none(message.tp3)
    if alert_type == TelegramAlertType.SL_HIT:
        return _decimal_or_none(message.stop_loss)
    return None


def _next_touched_watchlist_tp_from_price(
    repository: SQLiteTelegramAlertAttemptRepository,
    signal_id: str,
    current_price: Decimal,
    *,
    side: str,
    targets: Sequence[tuple[TelegramAlertType, Decimal]],
) -> TelegramAlertType | None:
    for alert_type, target in targets:
        if repository.has_attempt(signal_id=signal_id, alert_type=alert_type):
            continue
        if _tp_sl_price_condition(current_price, alert_type, side=side, level=target):
            return alert_type
    return None


def _live_price_snapshot(symbol_result: ScannerSymbolResult) -> tuple[WatchlistLivePriceSnapshot | None, str]:
    diagnostics = _representative_diagnostics(symbol_result)
    status_reason = _live_price_status_reason(diagnostics)
    if status_reason != NA:
        return None, status_reason
    raw_price = _live_price_candidate_value(symbol_result, diagnostics)
    price = _decimal_or_none(raw_price)
    if price is None:
        reason = "tp_sl_invalid_live_price" if _text(raw_price) != NA else "tp_sl_missing_live_price"
        return None, reason
    if price <= 0:
        return None, "tp_sl_invalid_live_price"
    source_symbol = _symbol(
        _first_non_na(
            *(diagnostics.get(key) for key in LIVE_PRICE_SYMBOL_KEYS),
            symbol_result.symbol,
        )
    )
    timestamp = _text(_first_non_na(*(diagnostics.get(key) for key in LIVE_PRICE_TIMESTAMP_KEYS)))
    return WatchlistLivePriceSnapshot(symbol=source_symbol, price=price, source="scanner_current_price", timestamp=timestamp), NA


def _live_price_candidate_value(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Any:
    return _first_non_na(
        *(diagnostics.get(key) for key in LIVE_PRICE_VALUE_KEYS),
        getattr(symbol_result, "current_price", NA),
    )


def _live_price_status_reason(diagnostics: Mapping[str, Any]) -> str:
    for key in LIVE_PRICE_STALE_FLAG_KEYS:
        if _truthy_live_price_flag(diagnostics.get(key)):
            return "tp_sl_stale_live_price"
    for key in LIVE_PRICE_STATUS_KEYS:
        status = _status_key(diagnostics.get(key))
        if status in LIVE_PRICE_BLOCKED_STATUS_KEYS:
            if status in {"missing", "unavailable"}:
                return "tp_sl_missing_live_price"
            if status in {"invalid", "nan", "zero"}:
                return "tp_sl_invalid_live_price"
            return "tp_sl_stale_live_price"
    for key in LIVE_PRICE_AGE_KEYS:
        age = _decimal_or_none(diagnostics.get(key))
        if age is not None and age > MAX_LIVE_PRICE_AGE_SECONDS:
            return "tp_sl_stale_live_price"
    for key in LIVE_PRICE_TIMESTAMP_KEYS:
        raw_timestamp = diagnostics.get(key)
        if _text(raw_timestamp) == NA:
            continue
        timestamp = _parse_live_price_timestamp(raw_timestamp)
        if timestamp is None:
            return "tp_sl_stale_live_price"
        age_seconds = Decimal(str((datetime.now(timezone.utc) - timestamp).total_seconds()))
        if age_seconds > MAX_LIVE_PRICE_AGE_SECONDS:
            return "tp_sl_stale_live_price"
    return NA


def _truthy_live_price_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    key = _status_key(value)
    return key in {"1", "true", "yes", "y", "stale", "expired"}


def _parse_live_price_timestamp(value: Any) -> datetime | None:
    text = _text(value)
    if text == NA:
        return None
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _persist_blocked_watchlist_tp_sl_attempt(
    repository: SQLiteTelegramAlertAttemptRepository,
    prior_alert: TelegramAlertAttemptRecord,
    *,
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
    reason: str,
    scan_run_id: str | None,
) -> TelegramLifecycleDelivery:
    _log_lifecycle_alert_audit(
        symbol_result=symbol_result,
        message=message,
        alert_type=alert_type,
        decision="blocked",
        reason=reason,
    )
    seen_at = now_utc_iso()
    transition = symbol_result.lifecycle_transition
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
    message_hash = hashlib.sha256(f"{prior_alert.signal_id}|{alert_type.value}|{reason}".encode("utf-8")).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=_blocked_alert_type(alert_type, reason),
        lifecycle_state=_lifecycle_state_text(symbol_result),
        sent_at=None,
        attempted_at=seen_at,
        telegram_status="blocked",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=alert_type.value,
        setup_quality_score=_quality_score(symbol_result),
        rr_planned=_text(message.planned_rr),
        min_rr=_text(message.min_rr),
        opportunity_score=_opportunity_score_text(symbol_result),
        min_score_for_idea=NA,
        technical_score=_technical_score_text(symbol_result),
        price_level=_price_level_for_alert(alert_type, message),
        **_message_level_metadata(message),
        blocked_reason=reason,
        invalid_target_fields=NA,
        error_message=reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=reason,
    )
    inserted = repository.insert_attempt(record)
    if not inserted:
        compacted = repository.compact_repeated_attempt(record)
        status = "blocked_repeat" if compacted else "duplicate"
        detail = (
            "Repeated blocked Telegram watchlist TP/SL attempt compacted."
            if compacted
            else "Duplicate blocked Telegram watchlist TP/SL attempt prevented."
        )
        return TelegramLifecycleDelivery(
            symbol=prior_alert.symbol,
            signal_id=prior_alert.signal_id,
            alert_type=alert_type.value,
            status=status,
            detail=detail,
            message_hash=message_hash,
            error_message=reason,
        )
    return TelegramLifecycleDelivery(
        symbol=prior_alert.symbol,
        signal_id=prior_alert.signal_id,
        alert_type=alert_type.value,
        status="blocked",
        detail="Telegram watchlist TP/SL lifecycle update blocked by live price guard.",
        message_hash=message_hash,
        error_message=reason,
    )


def _log_lifecycle_alert_audit(
    *,
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    alert_type: TelegramAlertType,
    decision: str,
    reason: str | None,
) -> None:
    diagnostics = _representative_diagnostics(symbol_result)
    current_price = _text(_live_price_candidate_value(symbol_result, diagnostics))
    log = logger.warning if _status_key(decision) in {"blocked", "blocked_repeat"} else logger.info
    log(
        (
            "Telegram lifecycle alert audit: symbol=%s lifecycle_state=%s direction=%s "
            "current_price=%s entry_low=%s entry_high=%s tp1=%s tp2=%s tp3=%s stop_loss=%s "
            "alert_type=%s decision=%s reason=%s public_decision=%s public_block_reason=%s "
            "actionable_grade_reason=%s confirmation_block_reason=%s lifecycle_promotion_reason=%s"
        ),
        _symbol(message.symbol),
        _lifecycle_state_text(symbol_result),
        _text(message.direction),
        current_price,
        format_telegram_price(message.entry_low),
        format_telegram_price(message.entry_high),
        format_telegram_price(message.tp1),
        format_telegram_price(message.tp2),
        format_telegram_price(message.tp3),
        format_telegram_price(message.stop_loss),
        alert_type.value,
        decision,
        _text(reason),
        decision if alert_type == TelegramAlertType.WATCHLIST else NA,
        _text(reason) if alert_type == TelegramAlertType.WATCHLIST else NA,
        _actionable_grade_reason_for_symbol(symbol_result),
        _text(reason) if "confirmation" in _status_key(reason) else NA,
        _lifecycle_promotion_reason(symbol_result),
    )


def _apply_soft_failed_confirmation_grace_to_decision(
    repository: SQLiteTelegramAlertAttemptRepository,
    symbol_result: ScannerSymbolResult,
    *,
    decision: TelegramAlertDecision,
    prior_alert: TelegramAlertAttemptRecord | None,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> TelegramAlertDecision | None:
    if (
        decision.alert_type != TelegramAlertType.NO_LONGER_TRACKING
        or decision.message is None
        or prior_alert is None
        or prior_alert.alert_type != TelegramAlertType.WATCHLIST.value
    ):
        return decision
    if _is_explicit_no_longer_tracking_state(symbol_result) and not _soft_failed_confirmation_lifecycle_evidence(
        symbol_result
    ):
        return decision
    if (
        _terminal_alert_type_for_lifecycle_state(_status_key(_lifecycle_state_text(symbol_result)))
        == TelegramAlertType.NO_LONGER_TRACKING
    ):
        blockers = _confirmed_guard_blockers(symbol_result, eligibility_context)
        if _soft_failed_confirmation_blocker_key(symbol_result, blockers) == NA:
            return decision
    else:
        blockers = _confirmed_guard_blockers(symbol_result, eligibility_context)

    blocker_key = _soft_failed_confirmation_blocker_key(symbol_result, blockers)
    if blocker_key == NA:
        return decision
    seen_count = _record_soft_failed_confirmation_observation(
        repository,
        prior_alert=prior_alert,
        symbol_result=symbol_result,
        blocker_key=blocker_key,
        blockers=blockers,
        scan_run_id=scan_run_id,
        eligibility_context=eligibility_context,
    )
    if seen_count < SOFT_FAILED_CONFIRMATION_MIN_OBSERVATIONS:
        return None
    return replace(
        decision,
        message=replace(
            decision.message,
            invalidation_reason=SOFT_FAILED_CONFIRMATION_REMOVAL_REASON,
        ),
    )


def _apply_soft_failed_confirmation_grace_to_reconciliation(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    prior_alert: TelegramAlertAttemptRecord,
    outcome: SentWatchlistReconciliationOutcome,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> SentWatchlistReconciliationOutcome | None:
    if outcome.alert_type != TelegramAlertType.NO_LONGER_TRACKING:
        return outcome
    if _is_explicit_no_longer_tracking_state(
        outcome.symbol_result
    ) and not _soft_failed_confirmation_lifecycle_evidence(outcome.symbol_result):
        return outcome
    blockers = _confirmed_guard_blockers(outcome.symbol_result, eligibility_context)
    blocker_key = _soft_failed_confirmation_blocker_key(outcome.symbol_result, blockers)
    if blocker_key == NA:
        return outcome
    seen_count = _record_soft_failed_confirmation_observation(
        repository,
        prior_alert=prior_alert,
        symbol_result=outcome.symbol_result,
        blocker_key=blocker_key,
        blockers=blockers,
        scan_run_id=scan_run_id,
        eligibility_context=eligibility_context,
    )
    if seen_count < SOFT_FAILED_CONFIRMATION_MIN_OBSERVATIONS:
        return None
    return SentWatchlistReconciliationOutcome(
        alert_type=outcome.alert_type,
        message=replace(
            outcome.message,
            invalidation_reason=SOFT_FAILED_CONFIRMATION_REMOVAL_REASON,
        ),
        symbol_result=outcome.symbol_result,
    )


def _record_soft_failed_confirmation_observation(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    prior_alert: TelegramAlertAttemptRecord,
    symbol_result: ScannerSymbolResult,
    blocker_key: str,
    blockers: Sequence[str],
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> int:
    alert_type = _soft_failed_confirmation_alert_type(blocker_key)
    existing = repository.get_attempt(signal_id=prior_alert.signal_id, alert_type=alert_type)
    seen_at = now_utc_iso()
    transition = symbol_result.lifecycle_transition
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
    reason = f"soft_failed_confirmation:{blocker_key}"
    message_hash = hashlib.sha256(
        f"{prior_alert.signal_id}|{SOFT_FAILED_CONFIRMATION_ATTEMPT}|{blocker_key}".encode("utf-8")
    ).hexdigest()
    message = telegram_signal_message_from_symbol(symbol_result)
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=alert_type,
        lifecycle_state=_lifecycle_state_text(symbol_result),
        sent_at=None,
        attempted_at=seen_at,
        telegram_status="skipped",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=SOFT_FAILED_CONFIRMATION_ATTEMPT,
        setup_quality_score=_quality_score(symbol_result),
        rr_planned=_text(message.planned_rr),
        min_rr=_text(eligibility_context.min_rr),
        opportunity_score=_opportunity_score_text(symbol_result),
        min_score_for_idea=_text(eligibility_context.min_score_for_idea),
        technical_score=_technical_score_text(symbol_result),
        price_level=_text(message.price_level),
        blocked_reason=reason,
        invalid_target_fields=_invalid_target_fields_from_reason("; ".join(str(blocker) for blocker in blockers)),
        error_message=reason,
        first_seen_at=existing.first_seen_at if existing is not None else seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=reason,
    )
    if existing is None:
        inserted = repository.insert_attempt(record)
        if inserted:
            return 1
    repository.compact_repeated_attempt(record)
    updated = repository.get_attempt(signal_id=prior_alert.signal_id, alert_type=alert_type)
    return updated.seen_count if updated is not None else 1


def _has_current_run_soft_failed_confirmation_observation(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    signal_id: str,
    scan_run_id: str | None,
) -> bool:
    if scan_run_id is None:
        return False
    return any(
        attempt.attempted_alert_type == SOFT_FAILED_CONFIRMATION_ATTEMPT
        and attempt.telegram_status == "skipped"
        and attempt.last_scan_run_id == scan_run_id
        for attempt in repository.list_attempts(signal_id=signal_id)
    )


def _confirmed_guard_blockers(
    symbol_result: ScannerSymbolResult,
    eligibility_context: TelegramEligibilityContext,
) -> tuple[str, ...]:
    message = replace(
        telegram_signal_message_from_symbol(symbol_result),
        min_rr=eligibility_context.min_rr,
    )
    blockers = (
        *_defensive_delivery_blockers(
            symbol_result,
            TelegramAlertType.SIGNAL_CONFIRMED,
            message,
            eligibility_context,
        ),
        *_failed_confirmation_core_blockers(symbol_result),
    )
    return tuple(dict.fromkeys(str(blocker) for blocker in blockers if _text(blocker) != NA))


def _soft_failed_confirmation_blocker_key(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
) -> str:
    if not blockers or _hard_failed_confirmation_blockers(symbol_result, blockers):
        return NA
    failed_gate = _status_key(_confirmed_rejection_context(symbol_result).active_failed_gate)
    if "regime_compatibility" in failed_gate:
        return "regime_compatibility"
    if "rr_below" in failed_gate or "low_rr" in failed_gate:
        return "rr_below_min"
    if "target_expansion" in failed_gate or "target_integrity" in failed_gate:
        return "target_expansion"
    haystack = " ".join(
        (
            _failed_confirmation_haystack(symbol_result, active_only=True),
            " ".join(str(blocker) for blocker in blockers),
        )
    )
    key = _status_key(haystack)
    if "regime_compatibility" in key or "cleaner_regime" in key or "regime_weakness" in key:
        return "regime_compatibility"
    if "target_expansion" in key or "target_integrity" in key or "not_enough_room" in key:
        return "target_expansion"
    if "rr_below" in key or "planned_rr_below" in key or "low_rr" in key or "risk_reward_below" in key:
        return "rr_below_min"
    if "opportunity_score" in key or "score_below" in key or "scoring" in key:
        return "score_below_min"
    if "technical_score" in key or "technical_quality" in key:
        return "technical_score"
    if "quality_gate" in key or "final_quality" in key or "setup_quality" in key:
        return "quality_gate"
    if "trade_idea_missing" in key or "missing_required_fields" in key:
        return "incomplete_confirmation"
    return NA


def _is_explicit_no_longer_tracking_state(symbol_result: ScannerSymbolResult) -> bool:
    return (
        _terminal_alert_type_for_lifecycle_state(_status_key(_lifecycle_state_text(symbol_result)))
        == TelegramAlertType.NO_LONGER_TRACKING
    )


def _soft_failed_confirmation_lifecycle_evidence(symbol_result: ScannerSymbolResult) -> bool:
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is None:
        return False
    rejection_context = _confirmed_rejection_context(symbol_result)
    haystack = _status_key(
        " ".join(
            _text(value)
            for value in (
                rejection_context.active_failed_gate,
                lifecycle.action_label if _text(rejection_context.active_failed_gate) != NA else NA,
                rejection_context.active_invalidation_reason,
                _failed_confirmation_haystack(symbol_result, active_only=True),
            )
            if _text(value) != NA
        )
    )
    return any(
        token in haystack
        for token in (
            "regime_compatibility",
            "cleaner_regime",
            "target_expansion",
            "target_integrity",
            "rr_below",
            "low_rr",
            "score_below",
            "technical_score",
            "quality_gate",
        )
    )


def _hard_failed_confirmation_blockers(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
) -> bool:
    if _failed_confirmation_is_structural_invalidation(symbol_result, blockers):
        return True
    invalid_target_fields = _invalid_target_fields_from_reason("; ".join(str(blocker) for blocker in blockers))
    if invalid_target_fields != NA and _target_integrity_has_numeric_terminal_failure(symbol_result):
        return True
    haystack = " ".join(
        (
            _failed_confirmation_haystack(symbol_result, active_only=True),
            " ".join(str(blocker) for blocker in blockers),
        )
    )
    key = _status_key(haystack)
    return any(
        token in key
        for token in (
            "accepted_beyond_invalidation",
            "acceptance_beyond_invalidation",
            "body_acceptance_failure",
            "invalidation_broken",
            "stop_broken",
            "wrong_side_target",
            "non_monotonic",
            "impossible_target",
        )
    )


def _target_integrity_has_numeric_terminal_failure(symbol_result: ScannerSymbolResult) -> bool:
    message = telegram_signal_message_from_symbol(symbol_result)
    side = _status_key(message.direction)
    if side not in {"long", "short"}:
        return False
    entry_reference = _entry_reference(symbol_result, message)
    if entry_reference is None:
        return False

    stop_loss = _decimal_or_none(message.stop_loss)
    if stop_loss is not None:
        if side == "long" and stop_loss >= entry_reference:
            return True
        if side == "short" and stop_loss <= entry_reference:
            return True

    targets = tuple(
        value
        for value in (
            _decimal_or_none(message.tp1),
            _decimal_or_none(message.tp2),
            _decimal_or_none(message.tp3),
        )
        if value is not None
    )
    for target in targets:
        if side == "long" and target <= entry_reference:
            return True
        if side == "short" and target >= entry_reference:
            return True
    for left, right in zip(targets, targets[1:]):
        if side == "long" and left >= right:
            return True
        if side == "short" and left <= right:
            return True
    return False


def _soft_failed_confirmation_alert_type(blocker_key: str) -> str:
    digest = hashlib.sha256(_status_key(blocker_key).encode("utf-8")).hexdigest()[:12]
    return f"{SOFT_FAILED_CONFIRMATION_ATTEMPT}_{digest}"


def _match_sent_watchlist_lifecycle(
    prior_alert: TelegramAlertAttemptRecord,
    *,
    lifecycle_repository: SQLiteSetupLifecycleRepository,
    current_results: Sequence[ScannerSymbolResult],
    snapshot_watchlists_by_symbol: Mapping[str, tuple[TelegramAlertAttemptRecord, ...]],
) -> SentWatchlistLifecycleMatch:
    exact = lifecycle_repository.get_record_by_lifecycle_id(prior_alert.signal_id)
    if exact is not None:
        return SentWatchlistLifecycleMatch(record=exact)

    current_exact = _current_lifecycle_records_for_signal_id(prior_alert.signal_id, current_results)
    if len(current_exact) == 1:
        return SentWatchlistLifecycleMatch(record=current_exact[0])
    if len(current_exact) > 1:
        return SentWatchlistLifecycleMatch(blocked_reason=SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS)

    active_watchlists = snapshot_watchlists_by_symbol.get(_symbol(prior_alert.symbol), ())
    if len(active_watchlists) != 1:
        reason = (
            SENT_WATCHLIST_RECONCILIATION_NO_MATCH
            if not active_watchlists
            else SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS
        )
        return SentWatchlistLifecycleMatch(blocked_reason=reason)
    if active_watchlists[0].signal_id != prior_alert.signal_id:
        return SentWatchlistLifecycleMatch(blocked_reason=SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS)

    fallback_records = _fallback_lifecycle_records_for_prior_alert(lifecycle_repository, prior_alert)
    if len(fallback_records) == 1:
        return SentWatchlistLifecycleMatch(record=fallback_records[0])
    if len(fallback_records) > 1:
        return SentWatchlistLifecycleMatch(blocked_reason=SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS)
    return SentWatchlistLifecycleMatch(blocked_reason=SENT_WATCHLIST_RECONCILIATION_NO_MATCH)


def _sent_watchlist_snapshot_by_symbol(
    sent_watchlists: Sequence[TelegramAlertAttemptRecord],
) -> dict[str, tuple[TelegramAlertAttemptRecord, ...]]:
    grouped: dict[str, list[TelegramAlertAttemptRecord]] = {}
    for prior_alert in sent_watchlists:
        if prior_alert.alert_type != TelegramAlertType.WATCHLIST.value or prior_alert.telegram_status != "sent":
            continue
        grouped.setdefault(_symbol(prior_alert.symbol), []).append(prior_alert)
    return {symbol: tuple(records) for symbol, records in grouped.items()}


def _current_result_for_prior_watchlist(
    prior_alert: TelegramAlertAttemptRecord,
    current_results: Sequence[ScannerSymbolResult],
) -> ScannerSymbolResult | None:
    exact_matches = tuple(
        symbol_result
        for symbol_result in current_results
        if prior_alert.signal_id in _signal_id_candidates(symbol_result)
    )
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return None

    symbol_matches = [
        symbol_result
        for symbol_result in current_results
        if _symbol(symbol_result.symbol) == _symbol(prior_alert.symbol)
    ]
    prior_direction = _status_key(prior_alert.direction)
    if prior_direction in {"long", "short"}:
        symbol_matches = [
            symbol_result
            for symbol_result in symbol_matches
            if _status_key(telegram_signal_message_from_symbol(symbol_result).direction) in {prior_direction, ""}
        ]
    return symbol_matches[0] if len(symbol_matches) == 1 else None


def _watchlist_candle_snapshot(symbol_result: ScannerSymbolResult) -> WatchlistCandleSnapshot | None:
    diagnostics = _representative_diagnostics(symbol_result)
    for key in ("current_candle", "latest_candle", "candle"):
        snapshot = _candle_snapshot_from_value(diagnostics.get(key))
        if snapshot is not None:
            return snapshot

    for high_key, low_key in (
        ("candle_high", "candle_low"),
        ("latest_high", "latest_low"),
        ("current_high", "current_low"),
        ("high", "low"),
    ):
        snapshot = _candle_snapshot_from_high_low(diagnostics.get(high_key), diagnostics.get(low_key))
        if snapshot is not None:
            return snapshot

    snapshot = _candle_snapshot_from_high_low(
        getattr(symbol_result, "latest_high", NA),
        getattr(symbol_result, "latest_low", NA),
    )
    if snapshot is not None:
        return snapshot

    for key in ("candles_5m", "candles_15m", "candles_1h", "candles_4h", "candles_12h", "candles_2d", "candles"):
        values = diagnostics.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray, Mapping)) or not values:
            continue
        snapshot = _candle_snapshot_from_value(values[-1])
        if snapshot is not None:
            return snapshot
    current_price = _first_non_na(
        diagnostics.get("current_price"),
        diagnostics.get("price"),
        diagnostics.get("last_price"),
        getattr(symbol_result, "current_price", NA),
        getattr(symbol_result, "latest_close", NA),
    )
    snapshot = _candle_snapshot_from_high_low(current_price, current_price)
    if snapshot is not None:
        return snapshot
    return None


def _candle_snapshot_from_value(value: Any) -> WatchlistCandleSnapshot | None:
    if isinstance(value, Mapping):
        high = _decimal_or_none(value.get("high"))
        low = _decimal_or_none(value.get("low"))
        identity = _first_non_na(
            value.get("timestamp"),
            value.get("opened_at"),
            value.get("open_time"),
            value.get("time"),
        )
    else:
        high = _decimal_or_none(getattr(value, "high", NA))
        low = _decimal_or_none(getattr(value, "low", NA))
        identity = _first_non_na(
            getattr(value, "timestamp", NA),
            getattr(value, "opened_at", NA),
            getattr(value, "open_time", NA),
            getattr(value, "time", NA),
        )
    if high is None or low is None:
        return None
    return WatchlistCandleSnapshot(high=max(high, low), low=min(high, low), identity=_text(identity))


def _candle_snapshot_from_high_low(high: Any, low: Any) -> WatchlistCandleSnapshot | None:
    high_value = _decimal_or_none(high)
    low_value = _decimal_or_none(low)
    if high_value is None or low_value is None:
        return None
    return WatchlistCandleSnapshot(high=max(high_value, low_value), low=min(high_value, low_value))


def _limit_zone_values(message: TelegramSignalMessage) -> tuple[Decimal, Decimal] | None:
    watch_pair = _decimal_pair_text(message.watch_zone)
    if watch_pair is not None:
        low, high = watch_pair
        return min(low, high), max(low, high)

    low = _decimal_or_none(message.entry_low)
    high = _decimal_or_none(message.entry_high)
    if low is not None and high is not None:
        return min(low, high), max(low, high)
    single = low if low is not None else high
    if single is not None:
        return single, single
    return None


def _candle_touches_zone(candle: WatchlistCandleSnapshot, zone: tuple[Decimal, Decimal]) -> bool:
    low, high = zone
    return entry_zone_touched(candle.high, candle.low, low, high)


def _valid_watchlist_stop(message: TelegramSignalMessage, zone: tuple[Decimal, Decimal]) -> Decimal | None:
    stop_loss = _decimal_or_none(message.stop_loss)
    if stop_loss is None:
        return None
    side = _status_key(message.direction)
    reference = (zone[0] + zone[1]) / Decimal("2")
    if side == "long" and stop_loss >= reference:
        return None
    if side == "short" and stop_loss <= reference:
        return None
    return stop_loss


def _valid_watchlist_targets(
    message: TelegramSignalMessage,
    zone: tuple[Decimal, Decimal],
) -> tuple[tuple[TelegramAlertType, Decimal], bool]:
    side = _status_key(message.direction)
    reference = (zone[0] + zone[1]) / Decimal("2")
    raw_targets = (
        (TelegramAlertType.TP1_HIT, _decimal_or_none(message.tp1)),
        (TelegramAlertType.TP2_HIT, _decimal_or_none(message.tp2)),
        (TelegramAlertType.TP3_HIT, _decimal_or_none(message.tp3)),
    )
    present = tuple((alert_type, target) for alert_type, target in raw_targets if target is not None)
    missing = len(present) != len(raw_targets)
    if not present or side not in {"long", "short"}:
        return (), True

    invalid = False
    for _alert_type, target in present:
        if side == "long" and target <= reference:
            invalid = True
        if side == "short" and target >= reference:
            invalid = True
    for (_, left), (_, right) in zip(present, present[1:]):
        if side == "long" and left >= right:
            invalid = True
        if side == "short" and left <= right:
            invalid = True
    return (() if invalid else present), missing or invalid


def _same_candle_touches_post_limit_outcome(
    candle: WatchlistCandleSnapshot,
    *,
    side: str,
    stop_loss: Decimal | None,
    targets: Sequence[tuple[TelegramAlertType, Decimal]],
) -> bool:
    if stop_loss is not None and _stop_touched(candle, side=side, stop_loss=stop_loss):
        return True
    return any(_target_touched(candle, side=side, target=target) for _alert_type, target in targets)


def _stop_touched(candle: WatchlistCandleSnapshot, *, side: str, stop_loss: Decimal) -> bool:
    if side == "long":
        return candle.low <= stop_loss
    if side == "short":
        return candle.high >= stop_loss
    return False


def _target_touched(candle: WatchlistCandleSnapshot, *, side: str, target: Decimal) -> bool:
    if side == "long":
        return candle.high >= target
    if side == "short":
        return candle.low <= target
    return False


def _next_touched_watchlist_tp(
    repository: SQLiteTelegramAlertAttemptRepository,
    signal_id: str,
    candle: WatchlistCandleSnapshot,
    *,
    side: str,
    targets: Sequence[tuple[TelegramAlertType, Decimal]],
) -> TelegramAlertType | None:
    for alert_type, target in targets:
        if repository.has_attempt(signal_id=signal_id, alert_type=alert_type):
            continue
        if _target_touched(candle, side=side, target=target):
            return alert_type
    return None


def _current_lifecycle_records_for_signal_id(
    signal_id: str,
    current_results: Sequence[ScannerSymbolResult],
) -> tuple[SetupLifecycleRecord, ...]:
    normalized = _identity(signal_id)
    if normalized == NA:
        return ()
    records: list[SetupLifecycleRecord] = []
    for symbol_result in current_results:
        lifecycle = symbol_result.lifecycle_state
        if lifecycle is None:
            continue
        candidates = _signal_id_candidates(symbol_result)
        if normalized in candidates or lifecycle.lifecycle_id == normalized:
            records.append(lifecycle)
    unique: dict[str, SetupLifecycleRecord] = {}
    for record in records:
        unique.setdefault(record.lifecycle_id, record)
    return tuple(unique.values())


def _fallback_lifecycle_records_for_prior_alert(
    lifecycle_repository: SQLiteSetupLifecycleRepository,
    prior_alert: TelegramAlertAttemptRecord,
) -> tuple[SetupLifecycleRecord, ...]:
    records = lifecycle_repository.list_records_for_symbol(symbol=prior_alert.symbol)
    direction = _status_key(prior_alert.direction)
    if direction not in {"long", "short"}:
        return records
    matched = tuple(
        record
        for record in records
        if _status_key(record.direction) in {direction, ""}
    )
    return matched


def _current_result_for_lifecycle_record(
    record: SetupLifecycleRecord,
    prior_alert: TelegramAlertAttemptRecord,
    current_results: Sequence[ScannerSymbolResult],
) -> ScannerSymbolResult | None:
    exact_matches = tuple(
        symbol_result
        for symbol_result in current_results
        if symbol_result.lifecycle_state is not None
        and (
            symbol_result.lifecycle_state.lifecycle_id == record.lifecycle_id
            or record.lifecycle_id in _signal_id_candidates(symbol_result)
            or prior_alert.signal_id in _signal_id_candidates(symbol_result)
        )
    )
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return None

    symbol_matches = [
        symbol_result
        for symbol_result in current_results
        if _symbol(symbol_result.symbol) == _symbol(record.symbol)
    ]
    prior_direction = _status_key(prior_alert.direction)
    if prior_direction in {"long", "short"}:
        symbol_matches = [
            symbol_result
            for symbol_result in symbol_matches
            if _status_key(telegram_signal_message_from_symbol(symbol_result).direction) in {prior_direction, ""}
        ]
    return symbol_matches[0] if len(symbol_matches) == 1 else None


def _sent_watchlist_reconciliation_outcome(
    prior_alert: TelegramAlertAttemptRecord,
    record: SetupLifecycleRecord,
    *,
    current_result: ScannerSymbolResult | None,
    eligibility_context: TelegramEligibilityContext,
) -> SentWatchlistReconciliationOutcome | None:
    state_key = _lifecycle_record_state_key(record)
    symbol_result = _reconciliation_symbol_result(record, prior_alert, current_result=current_result)
    terminal_alert_type = _terminal_alert_type_for_lifecycle_state(state_key)
    if terminal_alert_type is not None:
        message = replace(
            _message_with_prior_public_identity(
                _telegram_signal_message_for_alert(symbol_result, terminal_alert_type, eligibility_context),
                prior_alert,
            ),
            was_watchlist=prior_alert.alert_type == TelegramAlertType.WATCHLIST.value,
        )
        return SentWatchlistReconciliationOutcome(
            alert_type=terminal_alert_type,
            message=message,
            symbol_result=symbol_result,
        )

    failed_blockers = _lifecycle_failed_confirmation_blockers(
        symbol_result,
        confirmed_state=state_key == "confirmed",
    )
    if state_key == "confirmed":
        if failed_blockers:
            return _failed_confirmed_reconciliation_outcome(
                prior_alert,
                symbol_result,
                blockers=failed_blockers,
                eligibility_context=eligibility_context,
            )
        if current_result is None:
            return None
        return _confirmed_reconciliation_outcome(
            prior_alert,
            symbol_result,
            eligibility_context=eligibility_context,
        )

    if state_key in {"watchlist", "watchlisted", "stalking", "triggered", "rejected"} and failed_blockers:
        return _failed_confirmed_reconciliation_outcome(
            prior_alert,
            symbol_result,
            blockers=failed_blockers,
            eligibility_context=eligibility_context,
        )
    return None


def _confirmed_reconciliation_outcome(
    prior_alert: TelegramAlertAttemptRecord,
    symbol_result: ScannerSymbolResult,
    *,
    eligibility_context: TelegramEligibilityContext,
) -> SentWatchlistReconciliationOutcome:
    confirmed_message = replace(
        telegram_signal_message_from_symbol(symbol_result),
        min_rr=eligibility_context.min_rr,
    )
    blockers = _defensive_delivery_blockers(
        symbol_result,
        TelegramAlertType.SIGNAL_CONFIRMED,
        confirmed_message,
        eligibility_context,
    )
    if blockers:
        return _failed_confirmed_reconciliation_outcome(
            prior_alert,
            symbol_result,
            blockers=blockers,
            eligibility_context=eligibility_context,
        )
    message = replace(
        _message_with_prior_public_identity(confirmed_message, prior_alert),
        upgraded_from_watchlist=prior_alert.alert_type == TelegramAlertType.WATCHLIST.value,
    )
    return SentWatchlistReconciliationOutcome(
        alert_type=TelegramAlertType.SIGNAL_CONFIRMED,
        message=message,
        symbol_result=symbol_result,
    )


def _failed_confirmed_reconciliation_outcome(
    prior_alert: TelegramAlertAttemptRecord,
    symbol_result: ScannerSymbolResult,
    *,
    blockers: Sequence[str],
    eligibility_context: TelegramEligibilityContext,
) -> SentWatchlistReconciliationOutcome:
    message = replace(
        telegram_signal_message_from_symbol(symbol_result),
        min_rr=eligibility_context.min_rr,
        invalidation_reason=_failed_confirmation_reason(
            symbol_result,
            blockers,
            TelegramAlertType.NO_LONGER_TRACKING,
        ),
    )
    return SentWatchlistReconciliationOutcome(
        alert_type=TelegramAlertType.NO_LONGER_TRACKING,
        message=replace(
            _message_with_prior_public_identity(message, prior_alert),
            was_watchlist=prior_alert.alert_type == TelegramAlertType.WATCHLIST.value,
        ),
        symbol_result=symbol_result,
    )


def _reconciliation_symbol_result(
    record: SetupLifecycleRecord,
    prior_alert: TelegramAlertAttemptRecord,
    *,
    current_result: ScannerSymbolResult | None,
) -> ScannerSymbolResult:
    transition = _reconciliation_transition(record)
    if current_result is not None:
        return current_result.model_copy(
            update={
                "symbol": record.symbol,
                "lifecycle_state": record,
                "lifecycle_transition": transition,
            }
        )

    direction = _first_non_na(record.direction, prior_alert.direction)
    mode = _first_non_na(record.mode, NA)
    diagnostics = {
        "mode": mode,
        "direction": direction,
        "bias": direction,
        "failed_gate": record.failed_gate,
        "first_failed_gate": record.failed_gate,
        "action_label": record.action_label,
        "invalidation_reason": record.invalidation_reason,
        "reason": record.invalidation_reason,
        "regime_reason": record.invalidation_reason,
    }
    strategy_diagnostics = {mode: diagnostics} if mode != NA else {"lifecycle": diagnostics}
    return ScannerSymbolResult(
        symbol=record.symbol,
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics=strategy_diagnostics,
        valid_strategy_modes=(mode,) if mode != NA else (),
        technical_score=record.quality_score if record.quality_score else NA,
        lifecycle_state=record,
        lifecycle_transition=transition,
    )


def _reconciliation_transition(record: SetupLifecycleRecord) -> SetupTransitionResult:
    return SetupTransitionResult(
        lifecycle_id=record.lifecycle_id,
        symbol=record.symbol,
        from_state=record.previous_state,
        to_state=record.current_state,
        reason=SetupTransitionReason.NO_CHANGE,
        transitioned=True,
        record=record,
    )


def _terminal_alert_type_for_lifecycle_state(state_key: str) -> TelegramAlertType | None:
    if state_key == "invalidated":
        return TelegramAlertType.INVALIDATED
    if state_key == "expired":
        return TelegramAlertType.EXPIRED
    if state_key in {
        "cooldown",
        "cooled_down",
        "no_longer_tracking",
        "removed",
        "cancelled",
        "canceled",
    }:
        return TelegramAlertType.NO_LONGER_TRACKING
    return None


def _lifecycle_failed_confirmation_blockers(
    symbol_result: ScannerSymbolResult,
    *,
    confirmed_state: bool,
) -> tuple[str, ...]:
    blockers = _failed_confirmation_core_blockers(symbol_result)
    if confirmed_state:
        return blockers

    terminal_gate_keys = (
        "regime_compatibility",
        "target_integrity",
        "rr_below_min",
        "low_rr",
        "technical",
        "technical_score",
        "scoring",
        "opportunity_score",
        "quality_gate",
        "setup_rejected",
    )
    filtered: list[str] = []
    for blocker in blockers:
        key = _status_key(blocker)
        if key.startswith(("failed_confirmation_text", "failed_confirmation_action")):
            filtered.append(blocker)
        elif key.startswith("failed_confirmation_gate") and any(token in key for token in terminal_gate_keys):
            filtered.append(blocker)
    return tuple(dict.fromkeys(filtered))


def _lifecycle_record_state_key(record: SetupLifecycleRecord) -> str:
    return _status_key(record.current_state.value)


def _blocked_delivery_detail(alert_type: TelegramAlertType) -> str:
    if alert_type == TelegramAlertType.WATCHLIST:
        return "Telegram watchlist alert blocked by public readiness guard."
    if alert_type == TelegramAlertType.LIMIT_HIT:
        return "Telegram limit-hit update blocked by public signal gate."
    if alert_type in TP_SL_ALERT_TYPES:
        return "Telegram TP/SL lifecycle update blocked by live price guard."
    if alert_type in TERMINAL_UPDATE_ALERT_TYPES:
        return "Telegram terminal lifecycle update blocked by public alert identity guard."
    return "Telegram confirmed alert blocked by defensive eligibility guard."


def _blocked_alert_type(alert_type: TelegramAlertType, reason: str) -> str:
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
    return f"{alert_type.value}_BLOCKED_{digest}"


def _watchlist_outcome_audit_alert_type(reason: str) -> str:
    digest = hashlib.sha256(_status_key(reason).encode("utf-8")).hexdigest()[:12]
    return f"{WATCHLIST_OUTCOME_TRACKING_ATTEMPT}_{digest}"


def _watchlist_terminal_suppression_alert_type(alert_type: TelegramAlertType) -> str:
    digest = hashlib.sha256(alert_type.value.encode("utf-8")).hexdigest()[:12]
    return f"{WATCHLIST_TERMINAL_SUPPRESSION_ATTEMPT}_{digest}"


def _watchlist_expiry_audit_alert_type(reason: str) -> str:
    digest = hashlib.sha256(_status_key(reason).encode("utf-8")).hexdigest()[:12]
    return f"{WATCHLIST_EXPIRY_ATTEMPT}_{digest}"


def _selected_setup(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Any | None:
    mode = _first_non_na(
        symbol_result.valid_strategy_modes[0] if symbol_result.valid_strategy_modes else NA,
        diagnostics.get("mode"),
        getattr(symbol_result.lifecycle_state, "mode", NA),
    )
    if mode == NA:
        return None
    result = symbol_result.strategy_results.get(str(mode))
    if result is None:
        return None
    return getattr(result, str(mode), None)


def _representative_diagnostics(symbol_result: ScannerSymbolResult) -> Mapping[str, Any]:
    for mode in symbol_result.valid_strategy_modes:
        diagnostics = symbol_result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for mode in symbol_result.rejected_strategy_modes:
        diagnostics = symbol_result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is not None:
        diagnostics = symbol_result.strategy_diagnostics.get(lifecycle.mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for diagnostics in symbol_result.strategy_diagnostics.values():
        if isinstance(diagnostics, Mapping):
            return diagnostics
    return {}


def _minimum_rr_context_for_symbol(
    symbol_result: ScannerSymbolResult,
    context: TelegramEligibilityContext,
) -> TelegramEligibilityContext:
    effective_minimum_rr = _decimal_or_none(
        _representative_diagnostics(symbol_result).get("effective_minimum_rr")
    )
    if effective_minimum_rr is None:
        return context
    return replace(context, min_rr=effective_minimum_rr)


def _terminal_identity_bridge(
    repository: SQLiteTelegramAlertAttemptRepository,
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType | None,
    *,
    watchlist_only: bool = False,
) -> TerminalIdentityBridge:
    if alert_type not in TERMINAL_UPDATE_ALERT_TYPES and not watchlist_only:
        return TerminalIdentityBridge(blocked_reason="terminal_update_not_terminal_state")

    message = telegram_signal_message_from_symbol(symbol_result)
    exact = repository.get_prior_public_alert(
        signal_ids=_signal_id_candidates(symbol_result),
    )
    if exact is not None and (not watchlist_only or exact.alert_type == TelegramAlertType.WATCHLIST.value):
        return TerminalIdentityBridge(prior_alert=exact)

    active = repository.list_active_prior_public_alerts(symbol=symbol_result.symbol)
    if watchlist_only:
        active = tuple(record for record in active if record.alert_type == TelegramAlertType.WATCHLIST.value)
    if not active:
        inactive = repository.list_prior_public_alerts(symbol=symbol_result.symbol)
        if watchlist_only:
            inactive = tuple(
                record for record in inactive if record.alert_type == TelegramAlertType.WATCHLIST.value
            )
        if len(inactive) == 1:
            return TerminalIdentityBridge(prior_alert=inactive[0])
        if len(inactive) > 1:
            return TerminalIdentityBridge(blocked_reason="terminal_update_identity_ambiguous")
        reason = (
            "terminal_update_not_public_tracked"
            if repository.has_prior_public_alert_for_symbol(symbol=symbol_result.symbol)
            else "terminal_update_no_prior_public_alert"
        )
        return TerminalIdentityBridge(blocked_reason=reason)

    terminal_direction = _status_key(message.direction)
    if terminal_direction in {"long", "short"}:
        direction_matches = tuple(
            record for record in active if _status_key(record.direction) == terminal_direction
        )
        if len(direction_matches) == 1:
            return TerminalIdentityBridge(prior_alert=direction_matches[0])
        if len(direction_matches) > 1:
            return TerminalIdentityBridge(blocked_reason="terminal_update_identity_ambiguous")
        return TerminalIdentityBridge(blocked_reason="terminal_update_identity_not_matched")

    if len(active) == 1:
        return TerminalIdentityBridge(prior_alert=active[0])
    return TerminalIdentityBridge(blocked_reason="terminal_update_identity_ambiguous")


def _message_with_prior_public_identity(
    message: TelegramSignalMessage,
    prior_alert: TelegramAlertAttemptRecord,
) -> TelegramSignalMessage:
    return replace(
        message,
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
    )


def _message_with_prior_public_plan(
    message: TelegramSignalMessage,
    prior_alert: TelegramAlertAttemptRecord,
) -> TelegramSignalMessage:
    watch_zone = prior_alert.price_level if _decimal_pair_text(prior_alert.price_level) is not None else NA
    return replace(
        _message_with_prior_public_identity(message, prior_alert),
        watch_zone=watch_zone,
        entry_low=prior_alert.entry_low,
        entry_high=prior_alert.entry_high,
        stop_loss=prior_alert.stop_loss,
        tp1=prior_alert.tp1,
        tp2=prior_alert.tp2,
        tp3=prior_alert.tp3,
    )


def _message_with_observed_watchlist_price(
    message: TelegramSignalMessage,
    *,
    alert_type: TelegramAlertType,
    candle: WatchlistCandleSnapshot,
    side: str,
) -> TelegramSignalMessage:
    observed_price = _observed_watchlist_outcome_price(alert_type, candle=candle, side=side)
    if observed_price is None:
        return message
    return replace(message, price_level=observed_price)


def _observed_watchlist_outcome_price(
    alert_type: TelegramAlertType,
    *,
    candle: WatchlistCandleSnapshot,
    side: str,
) -> Decimal | None:
    if alert_type in {TelegramAlertType.TP1_HIT, TelegramAlertType.TP2_HIT, TelegramAlertType.TP3_HIT}:
        if side == "long":
            return candle.high
        if side == "short":
            return candle.low
    if alert_type == TelegramAlertType.SL_HIT:
        if side == "long":
            return candle.low
        if side == "short":
            return candle.high
    return None


def _preferred_prior_active_record(
    records: Sequence[TelegramAlertAttemptRecord],
    signal_id_priority: Sequence[str],
) -> TelegramAlertAttemptRecord:
    priority = {signal_id: index for index, signal_id in enumerate(signal_id_priority)}
    alert_priority = {
        TelegramAlertType.WATCHLIST.value: 0,
        TelegramAlertType.SIGNAL_CONFIRMED.value: 1,
        TelegramAlertType.LIMIT_HIT.value: 2,
    }
    return sorted(
        records,
        key=lambda record: (
            priority.get(record.signal_id, len(priority)),
            alert_priority.get(record.alert_type, len(alert_priority)),
            record.id or 0,
        ),
    )[0]


def _unique_prior_public_records(
    records: Sequence[TelegramAlertAttemptRecord],
) -> tuple[TelegramAlertAttemptRecord, ...]:
    grouped: dict[str, list[TelegramAlertAttemptRecord]] = {}
    for record in records:
        grouped.setdefault(record.signal_id, []).append(record)
    preferred = [
        _preferred_prior_active_record(tuple(group), (signal_id,))
        for signal_id, group in grouped.items()
    ]
    return tuple(sorted(preferred, key=lambda record: record.id or 0))


def _signal_id_candidates(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    lifecycle_id = _lifecycle_signal_id(symbol_result)
    fallback_id = _fallback_signal_id(symbol_result)
    return tuple(dict.fromkeys(value for value in (lifecycle_id, fallback_id) if _text(value) != NA))


def _signal_id(symbol_result: ScannerSymbolResult) -> str:
    lifecycle_id = _lifecycle_signal_id(symbol_result)
    if lifecycle_id != NA:
        return lifecycle_id
    return _fallback_signal_id(symbol_result)


def _lifecycle_signal_id(symbol_result: ScannerSymbolResult) -> str:
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is not None and _text(lifecycle.lifecycle_id) != NA:
        return lifecycle.lifecycle_id
    return NA


def _fallback_signal_id(symbol_result: ScannerSymbolResult) -> str:
    lifecycle = symbol_result.lifecycle_state
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    direction = _first_non_na(
        getattr(trade_idea, "direction", NA) if trade_idea is not None else NA,
        getattr(lifecycle, "direction", NA),
        _field(setup, "bias"),
        diagnostics.get("bias"),
        diagnostics.get("direction"),
    )
    mode = _first_non_na(
        symbol_result.valid_strategy_modes[0] if symbol_result.valid_strategy_modes else NA,
        symbol_result.rejected_strategy_modes[0] if symbol_result.rejected_strategy_modes else NA,
        getattr(lifecycle, "mode", NA),
        diagnostics.get("mode"),
    )
    stable_parts = (
        symbol_result.symbol,
        direction,
        mode,
        _first_non_na(
            diagnostics.get("initial_sweep_level"),
            diagnostics.get("sweep_level"),
            diagnostics.get("swing_level"),
            diagnostics.get("ltf_swing_level"),
        ),
        _first_non_na(_field(setup, "entry_low"), diagnostics.get("entry_low"), _level_field(getattr(trade_idea, "entry_zone", None), "low")),
        _first_non_na(_field(setup, "entry_high"), diagnostics.get("entry_high"), _level_field(getattr(trade_idea, "entry_zone", None), "high")),
        _first_non_na(_field(setup, "stop"), diagnostics.get("stop"), _level_field(getattr(trade_idea, "stop_loss", None), "price")),
        _first_non_na(_field(setup, "tp1"), diagnostics.get("tp1"), _take_profit(trade_idea, 1)),
        _first_non_na(_field(setup, "tp2"), diagnostics.get("tp2"), _take_profit(trade_idea, 2)),
        _first_non_na(_field(setup, "tp3"), diagnostics.get("tp3"), _take_profit(trade_idea, 3)),
        _first_non_na(getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA, _field(setup, "invalidation"), diagnostics.get("invalidation")),
        _watch_failed_gate(symbol_result, diagnostics),
        _watch_reason(symbol_result, diagnostics),
        _first_non_na(
            diagnostics.get("lifecycle_source_timestamp"),
            diagnostics.get("setup_source_timestamp"),
            diagnostics.get("signal_source_timestamp"),
        ),
    )
    digest = hashlib.sha256("|".join(_text(part) for part in stable_parts).encode("utf-8")).hexdigest()[:20]
    return f"{symbol_result.symbol}-{digest}"


def _lifecycle_state_text(symbol_result: ScannerSymbolResult) -> str:
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is None:
        return NA
    return lifecycle.current_state.value


def _transition_scan_run_id(transition: SetupTransitionResult | None) -> str | None:
    if transition is None or transition.event is None:
        return None
    return transition.event.scan_run_id


def _quality_score(symbol_result: ScannerSymbolResult) -> str:
    return _text(getattr(symbol_result.setup_quality, "quality_score", NA))


def _opportunity_score_decimal(symbol_result: ScannerSymbolResult) -> Decimal | None:
    trade_idea = symbol_result.trade_idea
    diagnostics = _representative_diagnostics(symbol_result)
    score_result = symbol_result.score_result
    return _first_decimal(
        getattr(score_result, "total_score", NA) if score_result is not None else NA,
        getattr(trade_idea, "confidence_score", NA) if trade_idea is not None else NA,
        diagnostics.get("opportunity_score"),
        diagnostics.get("total_score"),
    )


def _opportunity_score_text(symbol_result: ScannerSymbolResult) -> str:
    score = _opportunity_score_decimal(symbol_result)
    return _text(score)


def _technical_score_decimal(symbol_result: ScannerSymbolResult) -> Decimal | None:
    return _decimal_or_none(symbol_result.technical_score)


def _technical_score_text(symbol_result: ScannerSymbolResult) -> str:
    score = _technical_score_decimal(symbol_result)
    return _text(score)


def _price_level_for_alert(alert_type: TelegramAlertType, message: TelegramSignalMessage) -> str:
    if alert_type == TelegramAlertType.WATCHLIST:
        zone = _limit_zone_values(message)
        if zone is not None:
            low, high = zone
            return f"{format_telegram_price(low)}-{format_telegram_price(high)}"
        return _entry_zone_text(message.entry_low, message.entry_high)
    if alert_type == TelegramAlertType.TP1_HIT:
        return _text(message.tp1)
    if alert_type == TelegramAlertType.TP2_HIT:
        return _text(message.tp2)
    if alert_type == TelegramAlertType.TP3_HIT:
        return _text(message.tp3)
    if alert_type == TelegramAlertType.SL_HIT:
        return _text(message.stop_loss)
    if alert_type == TelegramAlertType.LIMIT_HIT:
        zone = _limit_zone_values(message)
        if zone is not None:
            low, high = zone
            return f"{_text(low)}-{_text(high)}"
        return f"{_text(message.entry_low)}-{_text(message.entry_high)}"
    return _text(message.price_level)


def _message_level_metadata(message: TelegramSignalMessage | None) -> dict[str, str]:
    if message is None:
        return {}
    return {
        "entry_low": format_telegram_price(message.entry_low),
        "entry_high": format_telegram_price(message.entry_high),
        "stop_loss": format_telegram_price(message.stop_loss),
        "tp1": format_telegram_price(message.tp1),
        "tp2": format_telegram_price(message.tp2),
        "tp3": format_telegram_price(message.tp3),
    }


def _confirmation_needed(diagnostics: Mapping[str, Any]) -> str:
    failed_gate = _text(diagnostics.get("first_failed_gate"))
    if failed_gate == "missing_confirmation_structure_shift":
        return "5m BOS/CHoCH confirmation."
    if failed_gate != NA:
        return failed_gate
    return NA


def _public_invalidation_sentence(
    *,
    direction: Any,
    stop_loss: Any,
    entry_low: Any,
    entry_high: Any,
    raw_invalidation: Any,
) -> str:
    side = _status_key(direction)
    stop = format_telegram_price(stop_loss)
    if stop == NA:
        raw = _clean_public_sentence(raw_invalidation)
        return raw if raw != NA and not _looks_like_rejection_reason(raw) else NA
    if side == "long":
        return f"Invalid if price accepts below {stop}."
    if side == "short":
        return f"Invalid if price accepts above {stop}."
    return f"Invalid if price accepts beyond {stop}."


def _human_confluence_sentence(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    structure = _structure_confluence_text(diagnostics)
    ob_fvg = _ob_fvg_confluence_text(diagnostics)
    volume = _volume_confluence_text(symbol_result, diagnostics)
    derivatives = _derivatives_confluence_text(symbol_result, diagnostics)
    return " ".join(part for part in (structure, ob_fvg, volume, derivatives) if part != NA)


def _structure_confluence_text(diagnostics: Mapping[str, Any]) -> str:
    confirmation = _status_key(diagnostics.get("confirmation_structure_shift_status"))
    sweep = _status_key(diagnostics.get("execution_sweep_status"))
    if confirmation == "passed":
        return "Structure is confirmed by a clean LTF BOS/CHoCH."
    if sweep == "passed":
        return "Structure has a valid sweep, but LTF confirmation context is N/A."
    return "Structure confirmation is N/A."


def _ob_fvg_confluence_text(diagnostics: Mapping[str, Any]) -> str:
    raw = _status_key(_first_non_na(diagnostics.get("selected_zone_type"), diagnostics.get("ob_fvg_status")))
    if "fvg" in raw:
        return "Price is reacting from a valid FVG reaction."
    if "ob" in raw or "order_block" in raw:
        return "Price is reacting from a valid OB reaction."
    return "OB/FVG context is N/A."


def _volume_confluence_text(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    source = _status_key(_first_non_na(diagnostics.get("volume_profile_source"), symbol_result.volume_profile_source))
    if source == "estimated_from_candles":
        return "Volume is candle-estimated."
    if source != "":
        return f"Volume context uses {_plain_label(source)} data."
    return "Volume context is N/A."


def _derivatives_confluence_text(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    funding_source = _first_context_value(
        (diagnostics.get("funding_context"), ("funding_status", "status", "direction", "funding_direction", "severity")),
        (symbol_result.funding_status, ()),
    )
    oi_source = _first_context_value(
        (diagnostics.get("oi_context"), ("oi_direction", "direction", "open_interest_direction", "trend")),
        (symbol_result.oi_direction, ()),
    )
    funding = _funding_phrase(funding_source)
    oi = _oi_phrase(oi_source)
    if funding == NA and oi == NA:
        return "Derivatives context is N/A."
    if funding != NA and oi != NA:
        prefix = (
            "Derivatives are neutral"
            if funding == "funding is normal" and oi != "open interest is rising"
            else "Derivatives context"
        )
        return f"{prefix}: {funding} while {oi}, so follow-through still needs structure to hold."
    if funding != NA:
        return f"Derivatives context: {funding}."
    return f"Derivatives context: {oi}."


def _derivatives_status(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    parts = [
        _first_non_na(diagnostics.get("funding_context"), symbol_result.funding_status),
        _first_non_na(diagnostics.get("oi_context"), symbol_result.oi_direction),
        _first_non_na(diagnostics.get("derivatives_supports_trade"), symbol_result.price_oi_relationship),
    ]
    text = " / ".join(part for part in (_text(value) for value in parts) if part != NA)
    return text if text else NA


def _funding_phrase(value: Any) -> str:
    value = _context_value(value, "funding_status", "status", "direction", "funding_direction", "severity")
    key = _status_key(value)
    if key in {"", "na", "n_a"}:
        return NA
    if "normal" in key or "neutral" in key:
        return "funding is normal"
    if "positive" in key:
        return "funding is positive"
    if "negative" in key:
        return "funding is negative"
    if "elevated" in key:
        return "funding is elevated"
    if "extreme" in key:
        return "funding is extreme"
    return f"funding is {_plain_label(key)}"


def _oi_phrase(value: Any) -> str:
    value = _context_value(value, "oi_direction", "direction", "open_interest_direction", "trend")
    key = _status_key(value)
    if key in {"", "na", "n_a"}:
        return NA
    if key in {"falling", "decreasing", "down"}:
        return "open interest is falling"
    if key in {"rising", "increasing", "up"}:
        return "open interest is rising"
    if key in {"flat", "neutral", "stable"}:
        return "open interest is stable"
    return f"open interest is {_plain_label(key)}"


def _context_value(value: Any, *keys: str) -> Any:
    if hasattr(value, "model_dump") and not isinstance(value, Mapping):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        return value
    for key in keys:
        candidate = value.get(key, NA)
        if _text(candidate) != NA:
            return candidate
    return NA


def _first_context_value(*candidates: tuple[Any, tuple[str, ...]]) -> Any:
    for value, keys in candidates:
        candidate = _context_value(value, *keys)
        if _text(candidate) != NA:
            return candidate
    return NA


def _entry_zone_text(entry_low: Any, entry_high: Any) -> str:
    low = format_telegram_price(entry_low)
    high = format_telegram_price(entry_high)
    if low == NA and high == NA:
        return NA
    if low == high or high == NA:
        return low
    if low == NA:
        return high
    return f"{low}-{high}"


def _raw_invalidation_text(symbol_result: ScannerSymbolResult) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    lifecycle = symbol_result.lifecycle_state
    return _first_non_na(
        getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA,
        _field(setup, "invalidation"),
        diagnostics.get("invalidation"),
        diagnostics.get("invalidation_reason"),
        getattr(lifecycle, "invalidation_reason", NA),
    )


def _looks_like_rejection_reason(value: Any) -> bool:
    text = _text(value).lower()
    if text == NA.lower():
        return False
    return any(fragment in text for fragment in INVALIDATION_REJECTION_FRAGMENTS)


def _clean_public_sentence(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return NA
    if _looks_like_rejection_reason(text):
        return NA
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _plain_label(value: Any) -> str:
    text = _status_key(value)
    return text.replace("_", " ") if text else NA


def _take_profit(trade_idea: Any | None, target_number: int) -> Any:
    targets = getattr(trade_idea, "take_profits", ()) if trade_idea is not None else ()
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        return NA
    index = target_number - 1
    if index >= len(targets):
        return NA
    return getattr(targets[index], "price", NA)


def _min_rr_for_alert(alert_type: TelegramAlertType, context: TelegramEligibilityContext) -> Decimal:
    if alert_type == TelegramAlertType.WATCHLIST:
        return context.public_watchlist_min_rr
    return context.min_rr


def _level_field(level: Any | None, name: str) -> Any:
    if level is None:
        return NA
    return getattr(level, name, NA)


def _field(source: Any | None, name: str) -> Any:
    if source is None:
        return NA
    if isinstance(source, Mapping):
        return source.get(name, NA)
    return getattr(source, name, NA)


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key, NA) if isinstance(value, Mapping) else NA


def _first_non_na(*values: Any) -> Any:
    for value in values:
        if _text(value) != NA:
            return value
    return NA


def _sequence_or_single(value: Any) -> tuple[Any, ...]:
    if value is None or value == NA:
        return ()
    if isinstance(value, Mapping):
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return (value,)


def _clean_watch_text(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return NA
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _lower_first(value: Any) -> str:
    text = _clean_watch_text(value)
    if text == NA:
        return NA
    return text[:1].lower() + text[1:]


def _row_value(row: sqlite3.Row, key: str, default: Any = NA) -> Any:
    return row[key] if key in row.keys() else default


def _record_from_row(row: sqlite3.Row) -> TelegramAlertAttemptRecord:
    return TelegramAlertAttemptRecord(
        id=int(row["id"]),
        signal_id=row["signal_id"],
        symbol=row["symbol"],
        direction=row["direction"],
        previous_state=row["previous_state"],
        new_state=row["new_state"],
        alert_type=row["alert_type"],
        lifecycle_state=row["lifecycle_state"],
        sent_at=row["sent_at"],
        attempted_at=row["attempted_at"],
        telegram_status=row["telegram_status"],
        message_hash=row["message_hash"],
        scan_run_id=row["scan_run_id"],
        attempted_alert_type=row["attempted_alert_type"],
        setup_quality_score=row["setup_quality_score"],
        rr_planned=row["rr_planned"],
        min_rr=row["min_rr"],
        opportunity_score=row["opportunity_score"],
        min_score_for_idea=row["min_score_for_idea"],
        technical_score=row["technical_score"],
        price_level=row["price_level"],
        entry_low=row["entry_low"],
        entry_high=row["entry_high"],
        stop_loss=row["stop_loss"],
        tp1=row["tp1"],
        tp2=row["tp2"],
        tp3=row["tp3"],
        blocked_reason=row["blocked_reason"],
        invalid_target_fields=row["invalid_target_fields"],
        error_message=row["error_message"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        seen_count=int(row["seen_count"]),
        last_scan_run_id=row["last_scan_run_id"],
        last_error_message=row["last_error_message"],
        public_watchlist_plan_id=_row_value(row, "public_watchlist_plan_id"),
        public_watchlist_event_key=_row_value(row, "public_watchlist_event_key"),
        public_alert_event_type=_row_value(row, "public_alert_event_type"),
        normalized_entry_zone_low=_row_value(row, "normalized_entry_zone_low"),
        normalized_entry_zone_high=_row_value(row, "normalized_entry_zone_high"),
        normalized_invalidation=_row_value(row, "normalized_invalidation"),
        dedupe_status=_row_value(row, "dedupe_status"),
        dedupe_reason=_row_value(row, "dedupe_reason"),
    )


def _first_decimal(*values: Any) -> Decimal | None:
    for value in values:
        decimal = _decimal_or_none(value)
        if decimal is not None:
            return decimal
    return None


def _decimal_or_default(value: Any, default: Decimal) -> Decimal:
    parsed = _decimal_or_none(value)
    return parsed if parsed is not None else default


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _text(value)
    if text == NA:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _alert_type_text(value: TelegramAlertType | str) -> str:
    return value.value if isinstance(value, TelegramAlertType) else _text(value)


def _status_key(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _identity(value: Any) -> str:
    text = _text(value)
    return text if text != NA else NA


def _symbol(value: Any) -> str:
    text = _text(value)
    return text.upper() if text != NA else NA


def _text(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if isinstance(value, Mapping):
        return NA
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, bool):
        return NA
    text = " ".join(str(value).split())
    return text if text else NA


__all__ = [
    "CONFIRMED_PUBLIC_DELIVERY_POLICY_DISABLED_REASON",
    "PUBLIC_SIGNAL_POLICY_SETUP_ONLY",
    "PUBLIC_WATCHLIST_REGIME_PENDING_GATE_CODES",
    "PUBLIC_WATCHLIST_TIMING_PENDING_GATE_CODES",
    "RESEARCH_WATCH_SETUP_ONLY_POLICY_DISABLED_REASON",
    "CONFIRMED_SIGNAL_RR_PENDING",
    "REGIME_MARKET_CONDITION_PENDING",
    "TIMING_CONFIRMATION_PENDING",
    "ResearchWatchCandidate",
    "SQLiteTelegramAlertAttemptRepository",
    "TelegramAlertDecision",
    "TelegramAlertType",
    "TelegramEligibilityContext",
    "TelegramLifecycleDelivery",
    "TelegramLifecycleDeliveryService",
    "TelegramLifecycleDeliverySummary",
    "classify_failed_gate_code",
    "format_research_watch_alert",
    "reserve_public_watchlist_event",
    "normalize_failed_gate_code",
    "telegram_alert_decision_for_symbol",
    "telegram_signal_message_from_symbol",
]
