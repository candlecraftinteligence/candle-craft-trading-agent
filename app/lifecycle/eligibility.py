from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from app.analytics.public_signal_quality import MIN_PUBLIC_SIGNAL_GRADE, public_quality_decision
from app.data.dtos import NA
from app.lifecycle.outcome_policy import has_valid_stored_plan_geometry

_MISSING = object()

PUBLIC_WATCHLIST_MIN_RR = Decimal("3")
WATCH_STATE_KEYS = frozenset({"actionable_a_grade", "a_grade_actionable", "a_grade_actionable_target_caution"})
FIRST_SEEN_TRIGGERED_STATE_KEY = "triggered"
INTERNAL_TOUCH_STATE_KEYS = frozenset(
    {
        "entry_hit",
        "entry_touch",
        "entry_touched",
        "entry_zone_hit",
        "entry_zone_touch",
        "entry_zone_touched",
        "limit_hit",
        "limit_touch",
        "limit_touched",
        "limit_zone_hit",
        "limit_zone_touch",
        "limit_zone_touched",
    }
)
PUBLIC_SIGNAL_ELIGIBLE_STATE_KEYS = frozenset({"confirmed"})
PUBLIC_ACTIVE_STATE_KEYS = frozenset({"confirmed", "executing", "managing", "active"})
PUBLIC_WATCHLIST_ACTIONABLE_STATE_KEYS = frozenset(
    {"actionable_a_grade", "a_grade_actionable", "a_grade_actionable_target_caution"}
)
ACTIVE_SIGNAL_STATE_KEYS = PUBLIC_ACTIVE_STATE_KEYS
TERMINAL_STATE_KEYS = frozenset(
    {
        "invalidated",
        "cooldown",
        "cooled_down",
        "archived",
        "rejected",
        "expired",
        "sl_hit",
        "stop_hit",
        "tp_hit",
        "tp1_hit",
        "tp2_hit",
        "tp3_hit",
        "no_longer_tracking",
        "completed",
        "closed",
        "cancelled",
        "canceled",
        "removed",
    }
)

PUBLIC_BLOCKER_KEYS = frozenset(
    {
        "a_grade_blocked_by_entry_window",
        "a_grade_blocked_by_final_gates",
        "a_grade_blocked_by_scoring",
        "a_grade_blocked_by_target",
        "a_grade_blocked_by_trust",
        "below_min_public_grade",
        "blocked",
        "body_acceptance_failure",
        "core_status_blocked",
        "data_issue",
        "derivatives_conflict",
        "error",
        "failed",
        "failed_quality_gates",
        "funding_oi_guard",
        "invalid",
        "invalid_scanner_result",
        "invalid_target_fields",
        "missing_confirmation_structure_shift",
        "missing_confirmed_sweep",
        "no_edge",
        "no_setup",
        "no_trade",
        "no_valid_liquidity_grab_pullback_setup",
        "no_valid_setup",
        "quality_gate_failed",
        "quality_gates_failed",
        "reject",
        "rejected",
        "rejected_by_derivatives",
        "rejected_by_regime",
        "rejected_by_risk",
        "rejected_by_scoring",
        "rejected_by_technical",
        "rejected_no_edge",
        "regime_blocked",
        "weak_regime_fit",
        "hard_regime_block",
        "rr_expansion_needed",
        "wait_for_rr_expansion_above_minimum",
        "invalid_rr",
        "below_min_rr",
        "missing_stop",
        "missing_sl",
        "missing_target",
        "missing_entry",
        "missing_entry_zone",
        "missing_limit_zone",
        "missing_invalidation",
        "no_trade_plan",
        "trade_map_na",
        "rr_below_minimum",
        "rr_too_low",
        "scan_error",
        "scanned_no_setup",
        "scanner_error",
        "setup_quality_blocked",
        "strategy_rejected",
        "target_integrity_failed",
        "technical_score_below_min",
        "watchlist_not_public_ready",
    }
)
PUBLIC_WATCHLIST_ALLOWED_PENDING_BLOCKER_KEYS = frozenset(
    {
        "below_min_rr",
        "challenge_rr_below_3",
        "clean_confirmation_pending",
        "confirmation_pending",
        "entry_zone_not_hit",
        "entry_zone_not_touched",
        "first_seen_triggered_pre_confirmation",
        "limit_zone_not_hit",
        "limit_zone_not_touched",
        "missing_confirmation",
        "missing_confirmation_structure_shift",
        "no_setup",
        "pullback_pending",
        "rejected_by_scoring",
        "rr_below_minimum",
        "rr_too_low",
        "scanned_no_setup",
        "trigger_not_hit",
    }
)

PUBLIC_BLOCKER_TEXT = (
    "a-grade candidate, but blocked",
    "below_min_public_grade",
    "blocked by final scoring",
    "blocked by target integrity",
    "blocked by trust meter",
    "blocked by expired entry window",
    "core_status_blocked",
    "failed quality gates",
    "invalid target",
    "no edge",
    "no valid liquidity-grab pullback setup",
    "no valid liquidity grab pullback setup",
    "no valid setup",
    "quality gate failed",
    "rejected_by_regime",
    "rejected_by_technical",
    "rejected_no_edge",
    "regime blocked",
    "regime fit: weak",
    "trade map: n/a",
    "wait for rr expansion",
    "scanned_no_setup",
    "setup_quality_blocked",
    "target_integrity_failed",
    "watchlist_not_public_ready",
)


@dataclass(frozen=True)
class LifecycleEligibilityConfig:
    min_rr: Decimal = Decimal("3")
    public_watchlist_min_rr: Decimal = PUBLIC_WATCHLIST_MIN_RR
    min_public_grade: str = MIN_PUBLIC_SIGNAL_GRADE


@dataclass(frozen=True)
class ResearchWatchEligibilityConfig:
    min_quality: int = 60
    min_readiness: int = 50


def is_numeric_trade_value(value: Any) -> bool:
    return _decimal_or_none(value) is not None


def has_valid_direction(record: Any) -> bool:
    return _direction(record) in {"long", "short"}


def has_valid_trade_map(record: Any) -> bool:
    levels = _trade_levels(record)
    direction = _direction(record)
    if direction not in {"long", "short"}:
        return False
    required = (levels.entry_low, levels.entry_high, levels.stop_loss, levels.tp1)
    if any(value is None for value in required):
        return False
    assert levels.entry_low is not None
    assert levels.entry_high is not None
    assert levels.stop_loss is not None
    assert levels.tp1 is not None
    if levels.entry_low > levels.entry_high:
        return False
    if direction == "long":
        if levels.stop_loss >= levels.entry_low or levels.tp1 <= levels.entry_high:
            return False
        return _long_optional_target_order(levels.tp1, levels.tp2, levels.tp3)
    if levels.stop_loss <= levels.entry_high or levels.tp1 >= levels.entry_low:
        return False
    return _short_optional_target_order(levels.tp1, levels.tp2, levels.tp3)


def _has_public_watchlist_plan(record: Any) -> bool:
    levels = _trade_levels(record)
    direction = _direction(record)
    if direction not in {"long", "short"}:
        return False
    if levels.entry_low is None or levels.entry_high is None:
        return False
    if levels.tp1 is None:
        return False
    if levels.entry_low > levels.entry_high:
        return False
    invalidation = _first_field(
        record,
        "invalidation_level",
        "invalid_below",
        "invalid_above",
        "invalidation",
        "invalidation_reason",
    )
    if levels.stop_loss is None and _display(invalidation) == NA:
        return False
    if levels.stop_loss is None:
        return True
    if direction == "long":
        return levels.stop_loss < levels.entry_low
    return levels.stop_loss > levels.entry_high


def has_valid_rr(record: Any, min_rr: Decimal | str | int | float = Decimal("3")) -> bool:
    minimum = _decimal_or_none(min_rr)
    if minimum is None:
        minimum = Decimal("3")
    rr_value = _trusted_rr(record)
    if rr_value is None:
        rr_value = _computed_rr_to_tp1(record)
    return rr_value is not None and rr_value >= minimum


def has_public_quality(record: Any, min_public_grade: str = MIN_PUBLIC_SIGNAL_GRADE) -> bool:
    decision = public_quality_decision(
        grade_candidates=_grade_candidates(record),
        score_candidates=_score_candidates(record),
        min_grade=min_public_grade,
    )
    return decision.passed


def has_no_public_blockers(record: Any) -> bool:
    return _has_no_public_blockers(record, reject_invalidation_reason=True)


def _has_no_public_blockers(record: Any, *, reject_invalidation_reason: bool) -> bool:
    return not _public_blocker_reasons(record, reject_invalidation_reason=reject_invalidation_reason)


def _public_blocker_reasons(
    record: Any,
    *,
    reject_invalidation_reason: bool,
    allowed_keys: Collection[str] = (),
) -> tuple[str, ...]:
    reasons: list[str] = []
    allowed = frozenset(_status_key(value) for value in allowed_keys)
    if _boolish(_first_field(record, "target_integrity_failed")):
        reasons.append("target_integrity_failed")
    if _boolish(_first_field(record, "regime_blocked")):
        reasons.append("regime_blocked")
    invalidation_reason = _display(_first_field(record, "invalidation_reason"))
    if reject_invalidation_reason and invalidation_reason != NA:
        reasons.append("invalidation_reason_present")
    for value in _blocker_values(record):
        key = _status_key(value)
        if key in allowed:
            continue
        if key and (key in PUBLIC_BLOCKER_KEYS or key in TERMINAL_STATE_KEYS):
            reasons.append(key)
        text = _display(value).lower()
        if text != NA.lower() and any(fragment in text for fragment in PUBLIC_BLOCKER_TEXT):
            reasons.append(key or text)
    return tuple(dict.fromkeys(reasons))


def is_terminal_state(state: Any) -> bool:
    return _status_key(state) in TERMINAL_STATE_KEYS


def is_watch_state(state: Any) -> bool:
    key = _status_key(state)
    return key in WATCH_STATE_KEYS and key not in TERMINAL_STATE_KEYS


def is_active_signal_state(state: Any) -> bool:
    key = _status_key(state)
    return key in ACTIVE_SIGNAL_STATE_KEYS and key not in TERMINAL_STATE_KEYS


def is_public_signal_eligible_state(state: Any) -> bool:
    key = _status_key(state)
    return key in PUBLIC_SIGNAL_ELIGIBLE_STATE_KEYS and key not in TERMINAL_STATE_KEYS


def is_public_active_state(state: Any) -> bool:
    key = _status_key(state)
    return key in PUBLIC_ACTIVE_STATE_KEYS and key not in TERMINAL_STATE_KEYS


def is_internal_touch_state(state: Any) -> bool:
    return _status_key(state) in INTERNAL_TOUCH_STATE_KEYS


def requires_existing_public_signal_for_update(state: Any) -> bool:
    key = _status_key(state)
    if key in TERMINAL_STATE_KEYS:
        return True
    return key in PUBLIC_ACTIVE_STATE_KEYS or key in INTERNAL_TOUCH_STATE_KEYS


def _first_seen_triggered_pre_confirmation(record: Any) -> bool:
    state = _status_key(_current_state(record))
    if state != FIRST_SEEN_TRIGGERED_STATE_KEY:
        return False
    failed_gate = _status_key(_first_field(record, "failed_gate"))
    diagnostics = _representative_diagnostics(record)
    pending = _status_key(
        _first_field(
            record,
            "pending_confirmation_reason",
            "confirmation_needed",
            "next_trigger_needed",
        )
    )
    diagnostic_pending = ""
    for diagnostic_value in (
        diagnostics.get("first_failed_gate", _MISSING),
        diagnostics.get("failed_gate", _MISSING),
        diagnostics.get("confirmation_needed", _MISSING),
        diagnostics.get("next_trigger_needed", _MISSING),
    ):
        diagnostic_pending = _status_key(diagnostic_value)
        if diagnostic_pending:
            break
    values = {failed_gate, pending, diagnostic_pending}
    return any(
        value in PUBLIC_WATCHLIST_ALLOWED_PENDING_BLOCKER_KEYS or "confirm" in value
        for value in values
        if value
    )


def public_watchlist_eligible(record: Any, config: LifecycleEligibilityConfig | None = None) -> bool:
    allowed, _ = is_public_watchlist_candidate(record, config)
    return allowed


def is_public_watchlist_candidate(
    candidate: Any,
    config: LifecycleEligibilityConfig | None = None,
) -> tuple[bool, list[str]]:
    eligibility = config or LifecycleEligibilityConfig()
    reasons: list[str] = []
    if _has_archived_at(candidate):
        reasons.append("archived")
    state = _current_state(candidate)
    state_key = _status_key(state)
    actionability_key = _status_key(
        _first_field(candidate, "actionability_state", ("lifecycle_state", "actionability_state"))
    )
    if (
        state_key in PUBLIC_ACTIVE_STATE_KEYS
        or state_key in INTERNAL_TOUCH_STATE_KEYS
        or state_key not in PUBLIC_WATCHLIST_ACTIONABLE_STATE_KEYS
        and actionability_key not in PUBLIC_WATCHLIST_ACTIONABLE_STATE_KEYS
    ) or is_terminal_state(state):
        reasons.append(f"lifecycle_state_not_eligible:{state_key or 'missing'}")
    if _cooldown_active(candidate):
        reasons.append("cooldown")
    direction_ok = has_valid_direction(candidate)
    if not direction_ok:
        reasons.append("missing_direction")
    public_plan_ok = _has_public_watchlist_plan(candidate)
    if not public_plan_ok:
        reasons.append("invalid_or_missing_public_watchlist_plan")
    if not _has_valid_canonical_lifecycle_plan(candidate):
        reasons.append("invalid_stored_plan_geometry")

    rr_value = _trusted_rr(candidate)
    if rr_value is None:
        rr_value = _computed_rr_to_tp1(candidate)
    minimum_rr = _decimal_or_none(eligibility.public_watchlist_min_rr) or PUBLIC_WATCHLIST_MIN_RR
    rr_ok = rr_value is not None and rr_value >= minimum_rr
    if rr_value is None:
        reasons.append("missing_rr")
    elif rr_value < minimum_rr:
        reasons.append(f"below_min_rr:{_display(rr_value)}<{_display(minimum_rr)}")

    quality = public_quality_decision(
        grade_candidates=_grade_candidates(candidate),
        score_candidates=_score_candidates(candidate),
        min_grade=eligibility.min_public_grade,
    )
    if not quality.passed:
        reasons.append(quality.reason)

    allowed_blocker_keys = frozenset()
    reasons.extend(
        _public_blocker_reasons(
            candidate,
            reject_invalidation_reason=False,
            allowed_keys=allowed_blocker_keys,
        )
    )
    return (not reasons, list(dict.fromkeys(reasons)))


def active_signal_eligible(record: Any, config: LifecycleEligibilityConfig | None = None) -> bool:
    eligibility = config or LifecycleEligibilityConfig()
    if _has_archived_at(record):
        return False
    state = _current_state(record)
    if not is_active_signal_state(state) or is_terminal_state(state):
        return False
    if _cooldown_active(record):
        return False
    return (
        has_valid_direction(record)
        and has_valid_trade_map(record)
        and _has_valid_canonical_lifecycle_plan(record)
        and has_valid_rr(record, eligibility.min_rr)
        and has_public_quality(record, eligibility.min_public_grade)
        and _has_no_public_blockers(record, reject_invalidation_reason=False)
    )


def _has_valid_canonical_lifecycle_plan(record: Any) -> bool:
    if isinstance(record, Mapping) and "_stored_lifecycle_record" in record:
        lifecycle = record["_stored_lifecycle_record"]
        return lifecycle in (None, NA) or has_valid_stored_plan_geometry(lifecycle)
    lifecycle = _field(record, "lifecycle_state")
    if lifecycle is not None and lifecycle != NA and not isinstance(lifecycle, (str, bytes)):
        return has_valid_stored_plan_geometry(lifecycle)
    return has_valid_stored_plan_geometry(record)


def research_watch_eligible(record: Any, config: ResearchWatchEligibilityConfig | None = None) -> bool:
    eligibility = config or ResearchWatchEligibilityConfig()
    if _has_archived_at(record) or _cooldown_active(record):
        return False
    if _display(_first_field(record, "invalidated_at", "no_longer_tracking_at")) != NA:
        return False
    state = _current_state(record)
    if _display(state) != NA and is_terminal_state(state):
        return False
    if _status_key(_first_field(record, "status")) != "rejected_by_regime":
        return False
    if _status_key(_first_field(record, "display_bucket", "display_status")) != "near_miss":
        return False
    if _symbol_text(_first_field(record, "symbol")) == NA:
        return False
    if _display(_first_field(record, "next_trigger_needed", "action_label", ("setup_quality", "action_label"))) == NA:
        return False
    if _display(_first_field(record, "regime_state")) == NA:
        return False
    if _display(_first_field(record, "regime_compatibility_label")) == NA:
        return False
    quality = _integer_score(_first_field(record, "setup_quality_score", "quality_score", ("setup_quality", "quality_score")))
    readiness = _integer_score(_first_field(record, "readiness_score"))
    if quality < eligibility.min_quality or readiness < eligibility.min_readiness:
        return False
    return _research_regime_block_reason_present(record)


def admin_research_visible(record: Any) -> bool:
    state = _status_key(_current_state(record))
    display = _status_key(_first_field(record, "display_status", "display_bucket"))
    quality = _status_key(_first_field(record, "quality_grade_current", "setup_quality_score", "quality_grade"))
    return state in TERMINAL_STATE_KEYS or display in {"near_miss", "rejected", "no_setup"} or quality in {"reject", "rejected"}


@dataclass(frozen=True)
class _TradeLevels:
    entry_low: Decimal | None
    entry_high: Decimal | None
    stop_loss: Decimal | None
    tp1: Decimal | None
    tp2: Decimal | None
    tp3: Decimal | None


def _trade_levels(record: Any) -> _TradeLevels:
    return _TradeLevels(
        entry_low=_decimal_or_none(_preferred_field(record, "entry_low", "entry_zone_low", ("entry_zone", "low"))),
        entry_high=_decimal_or_none(_preferred_field(record, "entry_high", "entry_zone_high", ("entry_zone", "high"))),
        stop_loss=_decimal_or_none(_preferred_field(record, "stop_loss", "stop", "stop_price", ("stop_loss", "price"))),
        tp1=_decimal_or_none(_preferred_field(record, "tp1", "take_profit_1", "target_1", ("take_profits", 0, "price"))),
        tp2=_decimal_or_none(_preferred_field(record, "tp2", "take_profit_2", "target_2", ("take_profits", 1, "price"))),
        tp3=_decimal_or_none(_preferred_field(record, "tp3", "take_profit_3", "target_3", ("take_profits", 2, "price"))),
    )


def _long_optional_target_order(tp1: Decimal, tp2: Decimal | None, tp3: Decimal | None) -> bool:
    if tp2 is not None and tp2 <= tp1:
        return False
    if tp3 is not None and (tp2 is None or tp3 <= tp2):
        return False
    return True


def _short_optional_target_order(tp1: Decimal, tp2: Decimal | None, tp3: Decimal | None) -> bool:
    if tp2 is not None and tp2 >= tp1:
        return False
    if tp3 is not None and (tp2 is None or tp3 >= tp2):
        return False
    return True


def _trusted_rr(record: Any) -> Decimal | None:
    return _decimal_or_none(
        _first_field(record, "rr", "rr_planned", "planned_rr", "best_rr", "rr_to_tp2", ("setup_quality", "best_rr"))
    )


def _computed_rr_to_tp1(record: Any) -> Decimal | None:
    levels = _trade_levels(record)
    direction = _direction(record)
    if direction not in {"long", "short"}:
        return None
    if None in (levels.entry_low, levels.entry_high, levels.stop_loss, levels.tp1):
        return None
    assert levels.entry_low is not None
    assert levels.entry_high is not None
    assert levels.stop_loss is not None
    assert levels.tp1 is not None
    entry = (levels.entry_low + levels.entry_high) / Decimal("2")
    if direction == "long":
        risk = entry - levels.stop_loss
        reward = levels.tp1 - entry
    else:
        risk = levels.stop_loss - entry
        reward = entry - levels.tp1
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def _direction(record: Any) -> str:
    return _status_key(_preferred_field(record, "direction", "bias"))


def _current_state(record: Any) -> Any:
    return _first_field(
        record,
        "current_state",
        "lifecycle_current_state",
        ("lifecycle_state", "current_state"),
        ("lifecycle_state", "state"),
        "new_state",
        "state",
    )


def _has_archived_at(record: Any) -> bool:
    return _display(_first_field(record, "archived_at")) != NA


def _cooldown_active(record: Any) -> bool:
    value = _display(_first_field(record, "cooldown_until"))
    if value == NA:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) > datetime.now(UTC)


def _research_regime_block_reason_present(record: Any) -> bool:
    values = (
        _first_field(record, "rejection_reason"),
        _first_field(record, "failed_gate"),
        _first_field(record, "blocked_reason"),
        _first_field(record, ("setup_quality", "decision_reason")),
        *_sequence_fields(record, "rejection_reasons", "regime_notes", "environment_notes"),
    )
    for value in values:
        text = _display(value).lower()
        key = _status_key(value)
        if key in {"rejected_by_regime", "regime_compatibility", "regime_blocked"}:
            return True
        if text == NA.lower():
            continue
        if "hostile" in text:
            return True
        if "regime" in text and any(fragment in text for fragment in ("block", "weak", "hostile", "compatib", "reject")):
            return True
    return False


def _integer_score(value: Any) -> int:
    number = _decimal_or_none(value)
    if number is None:
        return 0
    return int(number)


def _symbol_text(value: Any) -> str:
    text = _display(value)
    return text.upper() if text != NA else NA


def _grade_candidates(record: Any) -> tuple[Any, ...]:
    return (
        _first_field(record, "candidate_quality_grade"),
        _first_field(record, ("lifecycle_state", "candidate_quality_grade")),
        _first_field(record, "final_quality_grade"),
        _first_field(record, ("lifecycle_state", "final_quality_grade")),
        _first_field(record, "quality_grade_current"),
        _first_field(record, "quality_grade_confirmed"),
        _first_field(record, "setup_quality_score"),
        _first_field(record, "quality_grade"),
        _first_field(record, "grade"),
        _first_field(record, "trust_grade"),
        _first_field(record, ("setup_quality", "quality_grade")),
        _first_field(record, ("trade_idea", "grade")),
    )


def _score_candidates(record: Any) -> tuple[Any, ...]:
    return (
        _first_field(record, "quality_score"),
        _first_field(record, "setup_quality_score"),
        _first_field(record, "opportunity_score"),
        _first_field(record, "confidence_score"),
        _first_field(record, "trust_percentage"),
        _first_field(record, ("setup_quality", "quality_score")),
        _first_field(record, ("trade_idea", "confidence_score")),
    )


def _blocker_values(record: Any) -> tuple[Any, ...]:
    diagnostics = _representative_diagnostics(record)
    direct = (
        _first_field(record, "actionability_state"),
        _first_field(record, ("lifecycle_state", "actionability_state")),
        _first_field(record, "final_failed_gate"),
        _first_field(record, ("lifecycle_state", "final_failed_gate")),
        _first_field(record, "final_block_reason"),
        _first_field(record, ("lifecycle_state", "final_block_reason")),
        _first_field(record, "public_block_reason"),
        _first_field(record, "failed_gate"),
        _first_field(record, "rejection_reason"),
        _first_field(record, "blocked_reason"),
        _first_field(record, "error_message"),
        _first_field(record, "last_error_message"),
        _first_field(record, "status"),
        _first_field(record, "display_status"),
        _first_field(record, "display_bucket"),
        _first_field(record, "quality_state"),
        _first_field(record, ("setup_quality", "quality_state")),
        _first_field(record, ("setup_quality", "decision_reason")),
        _first_field(record, ("trade_idea", "status")),
    )
    diagnostic_values = (
        diagnostics.get("actionability_state", NA),
        diagnostics.get("final_failed_gate", NA),
        diagnostics.get("final_block_reason", NA),
        diagnostics.get("public_block_reason", NA),
        diagnostics.get("first_failed_gate", NA),
        diagnostics.get("failed_gate", NA),
        diagnostics.get("rejection_reason", NA),
        diagnostics.get("core_status_blocked", NA),
        diagnostics.get("setup_quality_blocked", NA),
        diagnostics.get("watchlist_not_public_ready", NA),
        diagnostics.get("target_integrity_failed", NA),
        diagnostics.get("status", NA),
        diagnostics.get("display_status", NA),
        diagnostics.get("display_bucket", NA),
    )
    return (*direct, *diagnostic_values, *_sequence_fields(record, "rejection_reasons", "hard_rejection_reasons", "gates_failed"))


def _representative_diagnostics(record: Any) -> Mapping[str, Any]:
    diagnostics = _field(record, "strategy_diagnostics")
    if not isinstance(diagnostics, Mapping) and _display(diagnostics) == NA:
        diagnostics = _field(_field(record, "lifecycle_state"), "strategy_diagnostics")
    if not isinstance(diagnostics, Mapping) and _display(diagnostics) == NA:
        diagnostics = _field(_field(record, "trade_idea"), "strategy_diagnostics")
    if isinstance(diagnostics, Mapping):
        for value in diagnostics.values():
            if isinstance(value, Mapping):
                return value
        return diagnostics
    raw = _field(record, "raw_result")
    return raw if isinstance(raw, Mapping) else {}


def _sequence_fields(record: Any, *names: str) -> tuple[Any, ...]:
    values: list[Any] = []
    for name in names:
        value = _field(record, name)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str, Mapping)):
            values.extend(value)
    return tuple(values)


def _first_field(record: Any, *names: str | tuple[Any, ...]) -> Any:
    for name in names:
        value = _nested_field(record, name) if isinstance(name, tuple) else _field(record, name)
        if _display(value) != NA or isinstance(value, Mapping):
            return value
    lifecycle = _field(record, "lifecycle_state")
    if lifecycle is not record:
        for name in names:
            value = _nested_field(lifecycle, name) if isinstance(name, tuple) else _field(lifecycle, name)
            if _display(value) != NA or isinstance(value, Mapping):
                return value
    trade_idea = _field(record, "trade_idea")
    if trade_idea is not record:
        for name in names:
            value = _nested_field(trade_idea, name) if isinstance(name, tuple) else _field(trade_idea, name)
            if _display(value) != NA or isinstance(value, Mapping):
                return value
    diagnostics = _representative_diagnostics(record)
    for name in names:
        if isinstance(name, str):
            value = diagnostics.get(name, NA)
            if _display(value) != NA:
                return value
    return NA


def _preferred_field(record: Any, *names: str | tuple[Any, ...]) -> Any:
    direct = _first_existing_field(record, *names)
    if direct is not _MISSING:
        return direct
    lifecycle = _field(record, "lifecycle_state")
    if lifecycle is not record:
        lifecycle_value = _first_existing_field(lifecycle, *names)
        if lifecycle_value is not _MISSING:
            return lifecycle_value
    trade_idea = _field(record, "trade_idea")
    if trade_idea is not record:
        trade_value = _first_existing_field(trade_idea, *names)
        if trade_value is not _MISSING:
            return trade_value
    return _first_field(record, *names)


def _first_existing_field(record: Any, *names: str | tuple[Any, ...]) -> Any:
    for name in names:
        if isinstance(name, tuple):
            exists, value = _existing_nested_field(record, name)
        else:
            exists, value = _existing_field(record, name)
        if exists:
            return value
    return _MISSING


def _existing_nested_field(record: Any, path: tuple[Any, ...]) -> tuple[bool, Any]:
    current = record
    for segment in path:
        if isinstance(segment, int):
            if not isinstance(current, Sequence) or isinstance(current, (str, bytes, bytearray, Mapping)):
                return False, NA
            if len(current) <= segment:
                return False, NA
            current = current[segment]
        else:
            exists, current = _existing_field(current, str(segment))
            if not exists:
                return False, NA
        if current is None:
            return True, NA
    return True, current


def _existing_field(record: Any, name: str) -> tuple[bool, Any]:
    if record is None:
        return False, NA
    if isinstance(record, Mapping):
        return (name in record), record.get(name, NA)
    if hasattr(record, name):
        return True, getattr(record, name)
    return False, NA


def _nested_field(record: Any, path: tuple[Any, ...]) -> Any:
    current = record
    for segment in path:
        if isinstance(segment, int):
            if isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray, Mapping)):
                if len(current) <= segment:
                    return NA
                current = current[segment]
            else:
                return NA
        else:
            current = _field(current, str(segment))
        if current is None:
            return NA
    return current


def _field(record: Any, name: str) -> Any:
    if record is None:
        return NA
    if isinstance(record, Mapping):
        return record.get(name, NA)
    return getattr(record, name, NA)


def _decimal_or_none(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if hasattr(value, "value") and not isinstance(value, (str, int, float, Decimal)):
        value = value.value
    text = _display(value)
    if text == NA:
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _boolish(value: Any) -> bool:
    text = _status_key(value)
    return text not in {"", "na", "n_a", "none", "false", "0", "no", "passed", "pass"}


def _status_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _display(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if isinstance(value, Enum):
        value = value.value
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, Mapping):
        return NA
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return NA
    text = " ".join(str(value).split())
    if not text or text.upper() in {"NA", "N/A", "NAN", "NONE", "NULL"}:
        return NA
    return text


__all__ = [
    "ACTIVE_SIGNAL_STATE_KEYS",
    "LifecycleEligibilityConfig",
    "PUBLIC_WATCHLIST_MIN_RR",
    "PUBLIC_WATCHLIST_ACTIONABLE_STATE_KEYS",
    "ResearchWatchEligibilityConfig",
    "TERMINAL_STATE_KEYS",
    "WATCH_STATE_KEYS",
    "active_signal_eligible",
    "admin_research_visible",
    "has_no_public_blockers",
    "has_public_quality",
    "has_valid_direction",
    "has_valid_rr",
    "has_valid_trade_map",
    "is_active_signal_state",
    "is_internal_touch_state",
    "is_numeric_trade_value",
    "is_public_active_state",
    "is_public_signal_eligible_state",
    "is_public_watchlist_candidate",
    "is_terminal_state",
    "is_watch_state",
    "public_watchlist_eligible",
    "research_watch_eligible",
    "requires_existing_public_signal_for_update",
]
