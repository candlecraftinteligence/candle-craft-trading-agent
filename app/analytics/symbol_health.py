from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.data.dtos import NA
from app.lifecycle.models import (
    ACTIVE_LIFECYCLE_MONITORING_STATES,
    lifecycle_requires_market_observation,
)

DEFAULT_SYMBOL_HEALTH_SCORE = 70
DEFAULT_SYMBOL_COOLDOWN_MINUTES = 30.0
DEFAULT_MAX_TIMEOUT_STRIKES = 3
MAX_SLOW_SYMBOLS = 5

PRIORITY_LIFECYCLE_STATES = {
    "EXECUTING": 0,
    "CONFIRMED": 0,
    "TRIGGERED": 0,
    "MANAGING": 0,
}
WATCH_LIFECYCLE_STATES = {
    "STALKING": 1,
    "WATCHLISTED": 1,
}
ACTIVE_LIFECYCLE_PRIORITY = {
    state.value: rank for rank, state in enumerate(ACTIVE_LIFECYCLE_MONITORING_STATES)
}
HOT_READINESS_LABELS = {"HOT WATCH", "VALID SETUP"}
NEAR_MISS_BUCKETS = {"near_miss", "valid"}
LOW_VALUE_BUCKETS = {"no_setup", "data_issue"}


class SymbolHealthRecord(BaseModel):
    symbol: str
    successful_scans: int = 0
    timeout_count: int = 0
    data_issue_count: int = 0
    average_runtime_sec: float = 0.0
    last_success_at: str | None = None
    last_timeout_at: str | None = None
    current_health_score: int = DEFAULT_SYMBOL_HEALTH_SCORE
    cooldown_until: str | None = None
    timeout_strikes: int = 0
    last_priority_rank: int | None = None
    last_prioritized_at: str | None = None
    last_scanned_at: str | None = None
    last_data_issue_at: str | None = None
    last_display_bucket: str = NA
    last_readiness_label: str = NA
    useful_scan_count: int = 0
    rejected_count: int = 0
    last_rejected_at: str | None = None
    invalidation_count: int = 0
    expired_setup_count: int = 0
    rejected_setup_count: int = 0
    false_confirmation_count: int = 0
    malformed_setup_event_count: int = 0
    stop_breach_after_confirmation_count: int = 0
    duplicate_noisy_setup_count: int = 0

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = str(value).strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator("current_health_score")
    @classmethod
    def _bounded_health_score(cls, value: int) -> int:
        return _bounded_int(value)

    @field_validator(
        "successful_scans",
        "timeout_count",
        "data_issue_count",
        "timeout_strikes",
        "useful_scan_count",
        "rejected_count",
        "invalidation_count",
        "expired_setup_count",
        "rejected_setup_count",
        "false_confirmation_count",
        "malformed_setup_event_count",
        "stop_breach_after_confirmation_count",
        "duplicate_noisy_setup_count",
    )
    @classmethod
    def _non_negative_int(cls, value: int) -> int:
        return max(0, int(value or 0))

    @field_validator("average_runtime_sec")
    @classmethod
    def _non_negative_float(cls, value: float) -> float:
        return round(max(0.0, float(value or 0.0)), 3)


class SymbolPriorityDecision(BaseModel):
    symbol: str
    priority_rank: int | None = None
    health_score: int
    timeout_strikes: int = 0
    cooldown_until: str | None = None
    skipped_due_to_cooldown: bool = False
    cooldown_exempted: bool = False
    lifecycle_state: str = NA
    last_display_bucket: str = NA
    last_readiness_label: str = NA
    useful_scan_count: int = 0
    priority_reasons: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = str(value).strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized


class SymbolPriorityPlan(BaseModel):
    enabled: bool = False
    original_symbols: tuple[str, ...] = ()
    symbols_to_scan: tuple[str, ...] = ()
    skipped_symbols: tuple[str, ...] = ()
    decisions: tuple[SymbolPriorityDecision, ...] = ()

    model_config = ConfigDict(frozen=True)

    @property
    def prioritized_symbols_count(self) -> int:
        return len(self.symbols_to_scan)

    @property
    def cooldown_symbols_count(self) -> int:
        return len(self.skipped_symbols)

    @property
    def cooldown_exemptions_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.cooldown_exempted)

    def priority_by_symbol(self) -> dict[str, SymbolPriorityDecision]:
        return {decision.symbol: decision for decision in self.decisions}

    def to_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "prioritized_symbols": self.prioritized_symbols_count,
            "cooldown_symbols": self.cooldown_symbols_count,
            "skipped_due_to_cooldown": len(self.skipped_symbols),
            "priority_symbols": [decision.model_dump(mode="json") for decision in self.decisions],
            "active_lifecycle_cooldown_exemptions": self.cooldown_exemptions_count,
        }


def empty_symbol_priority_plan(symbols: Sequence[str], *, enabled: bool = False) -> SymbolPriorityPlan:
    normalized = _normalize_symbols(symbols)
    decisions = tuple(
        SymbolPriorityDecision(
            symbol=symbol,
            priority_rank=index + 1,
            health_score=DEFAULT_SYMBOL_HEALTH_SCORE,
            priority_reasons=("input_order",),
        )
        for index, symbol in enumerate(normalized)
    )
    return SymbolPriorityPlan(
        enabled=enabled,
        original_symbols=normalized,
        symbols_to_scan=normalized,
        decisions=decisions,
    )


def build_symbol_priority_plan(
    symbols: Sequence[str],
    health_records: Mapping[str, SymbolHealthRecord] | None = None,
    *,
    lifecycle_states: Mapping[str, str] | None = None,
    now: str | None = None,
    enabled: bool = True,
) -> SymbolPriorityPlan:
    original = _normalize_symbols(symbols)
    if not enabled:
        return empty_symbol_priority_plan(original, enabled=False)
    if not original:
        return SymbolPriorityPlan(enabled=True)

    records = health_records or {}
    state_by_symbol = {str(symbol).upper(): _display(state).upper() for symbol, state in (lifecycle_states or {}).items()}
    timestamp = now or now_utc_iso()
    active: list[tuple[tuple[int, int, int, int, int, int, str], str, SymbolPriorityDecision]] = []
    skipped: list[SymbolPriorityDecision] = []
    original_index = {symbol: index for index, symbol in enumerate(original)}

    for symbol in original:
        record = records.get(symbol) or default_symbol_health(symbol)
        lifecycle_state = state_by_symbol.get(symbol, NA)
        reasons = _priority_reasons(record, lifecycle_state)
        requires_monitoring = lifecycle_requires_market_observation(lifecycle_state)
        cooldown_is_active = cooldown_active(record.cooldown_until, timestamp)
        if requires_monitoring:
            reasons = tuple((*reasons, "active_lifecycle_monitoring"))

        if cooldown_is_active and not requires_monitoring:
            skipped.append(
                SymbolPriorityDecision(
                    symbol=symbol,
                    health_score=record.current_health_score,
                    timeout_strikes=record.timeout_strikes,
                    cooldown_until=record.cooldown_until,
                    skipped_due_to_cooldown=True,
                    lifecycle_state=lifecycle_state,
                    last_display_bucket=record.last_display_bucket,
                    last_readiness_label=record.last_readiness_label,
                    useful_scan_count=record.useful_scan_count,
                    priority_reasons=tuple((*reasons, "cooldown")),
                )
            )
            continue

        lifecycle_rank = _lifecycle_priority_rank(lifecycle_state)
        hot_rank = _hot_watch_rank(record)
        low_health_rank = 1 if record.current_health_score < 40 else 0
        stale_rank = _stale_rejected_rank(record, timestamp)
        sort_key = (
            lifecycle_rank,
            hot_rank,
            -record.current_health_score,
            original_index[symbol],
            -record.useful_scan_count,
            low_health_rank + stale_rank,
            symbol,
        )
        decision = SymbolPriorityDecision(
            symbol=symbol,
            health_score=record.current_health_score,
            timeout_strikes=record.timeout_strikes,
            cooldown_until=record.cooldown_until,
            lifecycle_state=lifecycle_state,
            last_display_bucket=record.last_display_bucket,
            cooldown_exempted=cooldown_is_active and requires_monitoring,
            last_readiness_label=record.last_readiness_label,
            useful_scan_count=record.useful_scan_count,
            priority_reasons=reasons or ("health_score",),
        )
        active.append((sort_key, symbol, decision))

    active.sort(key=lambda item: item[0])
    ranked_decisions: list[SymbolPriorityDecision] = []
    symbols_to_scan: list[str] = []
    for rank, (_sort_key, symbol, decision) in enumerate(active, start=1):
        symbols_to_scan.append(symbol)
        ranked_decisions.append(decision.model_copy(update={"priority_rank": rank}))

    skipped_symbols = tuple(decision.symbol for decision in skipped)
    ranked_decisions.extend(skipped)
    return SymbolPriorityPlan(
        enabled=True,
        original_symbols=original,
        symbols_to_scan=tuple(symbols_to_scan),
        skipped_symbols=skipped_symbols,
        decisions=tuple(ranked_decisions),
    )


def default_symbol_health(symbol: str) -> SymbolHealthRecord:
    return SymbolHealthRecord(symbol=symbol)


def update_symbol_health_records(
    existing_records: Mapping[str, SymbolHealthRecord],
    symbol_results: Sequence[Any],
    *,
    priority_by_symbol: Mapping[str, SymbolPriorityDecision] | None = None,
    now: str | None = None,
    cooldown_minutes: float = DEFAULT_SYMBOL_COOLDOWN_MINUTES,
    max_timeout_strikes: int = DEFAULT_MAX_TIMEOUT_STRIKES,
) -> dict[str, SymbolHealthRecord]:
    timestamp = now or now_utc_iso()
    updated: dict[str, SymbolHealthRecord] = {symbol: record for symbol, record in existing_records.items()}
    priority = priority_by_symbol or {}

    for symbol_result in symbol_results:
        status = getattr(symbol_result, "status", NA)
        if _status_key(getattr(status, "value", status)) == "not_run":
            continue
        symbol = _symbol_from_result(symbol_result)
        if symbol == NA:
            continue
        record = updated.get(symbol) or default_symbol_health(symbol)
        decision = priority.get(symbol)
        updated[symbol] = update_symbol_health_record(
            record,
            symbol_result,
            priority_decision=decision,
            now=timestamp,
            cooldown_minutes=cooldown_minutes,
            max_timeout_strikes=max_timeout_strikes,
        )

    for symbol, decision in priority.items():
        if symbol in updated and decision.skipped_due_to_cooldown:
            updated[symbol] = apply_priority_to_health_record(updated[symbol], decision, now=timestamp)

    return updated


def update_symbol_health_record(
    record: SymbolHealthRecord,
    symbol_result: Any,
    *,
    priority_decision: SymbolPriorityDecision | None = None,
    now: str | None = None,
    cooldown_minutes: float = DEFAULT_SYMBOL_COOLDOWN_MINUTES,
    max_timeout_strikes: int = DEFAULT_MAX_TIMEOUT_STRIKES,
) -> SymbolHealthRecord:
    timestamp = now or now_utc_iso()
    symbol = _symbol_from_result(symbol_result)
    if symbol == NA:
        symbol = record.symbol

    classification = classify_symbol_result(symbol_result)
    timed_out = classification["timed_out"]
    data_issue = classification["data_issue"]
    useful = classification["useful"]
    no_setup = classification["display_bucket"] == "no_setup"
    health_events = symbol_health_events_from_result(symbol_result, now=timestamp)
    event_counts = _event_counts(health_events)

    successful_scans = record.successful_scans
    timeout_count = record.timeout_count
    data_issue_count = record.data_issue_count
    timeout_strikes = record.timeout_strikes
    last_success_at = record.last_success_at
    last_timeout_at = record.last_timeout_at
    last_data_issue_at = record.last_data_issue_at
    rejected_count = record.rejected_count
    last_rejected_at = record.last_rejected_at
    invalidation_count = record.invalidation_count + event_counts.get("invalidation", 0)
    expired_setup_count = record.expired_setup_count + event_counts.get("expired_setup", 0)
    rejected_setup_count = record.rejected_setup_count + event_counts.get("rejected_setup", 0)
    false_confirmation_count = record.false_confirmation_count + event_counts.get("false_confirmation", 0)
    malformed_setup_event_count = record.malformed_setup_event_count + event_counts.get("malformed_setup_event", 0)
    stop_breach_after_confirmation_count = record.stop_breach_after_confirmation_count + event_counts.get(
        "stop_breach_after_confirmation",
        0,
    )
    duplicate_noisy_setup_count = record.duplicate_noisy_setup_count + event_counts.get("duplicate_noisy_setup", 0)

    if timed_out:
        timeout_count += 1
        timeout_strikes += 1
        last_timeout_at = timestamp
    elif data_issue:
        data_issue_count += 1
        last_data_issue_at = timestamp
    else:
        successful_scans += 1
        timeout_strikes = 0
        last_success_at = timestamp

    if no_setup:
        rejected_count += 1
        last_rejected_at = timestamp

    useful_scan_count = record.useful_scan_count + (1 if useful else 0)
    health_score = calculate_next_health_score(
        previous_score=record.current_health_score,
        timed_out=timed_out,
        data_issue=data_issue,
        useful=useful,
        no_setup=no_setup,
        lifecycle_state=classification["lifecycle_state"],
        readiness_label=classification["readiness_label"],
        timeout_strikes=timeout_strikes,
    )
    health_score = max(0, health_score - _event_health_penalty(event_counts))
    cooldown_until = record.cooldown_until
    if timed_out and timeout_strikes >= max(1, int(max_timeout_strikes)):
        cooldown_until = cooldown_expiry(timestamp, cooldown_minutes)
    elif not timed_out and not cooldown_active(cooldown_until, timestamp):
        cooldown_until = None

    average_runtime = update_average_runtime(
        record.average_runtime_sec,
        previous_count=record.successful_scans + record.timeout_count + record.data_issue_count,
        runtime_sec=_runtime_seconds(symbol_result),
    )

    updated = record.model_copy(
        update={
            "symbol": symbol,
            "successful_scans": successful_scans,
            "timeout_count": timeout_count,
            "data_issue_count": data_issue_count,
            "average_runtime_sec": average_runtime,
            "last_success_at": last_success_at,
            "last_timeout_at": last_timeout_at,
            "current_health_score": health_score,
            "cooldown_until": cooldown_until,
            "timeout_strikes": timeout_strikes,
            "last_scanned_at": timestamp,
            "last_data_issue_at": last_data_issue_at,
            "last_display_bucket": classification["display_bucket"],
            "last_readiness_label": classification["readiness_label"],
            "useful_scan_count": useful_scan_count,
            "rejected_count": rejected_count,
            "last_rejected_at": last_rejected_at,
            "invalidation_count": invalidation_count,
            "expired_setup_count": expired_setup_count,
            "rejected_setup_count": rejected_setup_count,
            "false_confirmation_count": false_confirmation_count,
            "malformed_setup_event_count": malformed_setup_event_count,
            "stop_breach_after_confirmation_count": stop_breach_after_confirmation_count,
            "duplicate_noisy_setup_count": duplicate_noisy_setup_count,
        }
    )
    if priority_decision is not None:
        updated = apply_priority_to_health_record(updated, priority_decision, now=timestamp)
    return updated


def apply_priority_to_health_record(
    record: SymbolHealthRecord,
    decision: SymbolPriorityDecision,
    *,
    now: str | None = None,
) -> SymbolHealthRecord:
    return record.model_copy(
        update={
            "last_priority_rank": decision.priority_rank,
            "last_prioritized_at": now or now_utc_iso(),
        }
    )


def classify_symbol_result(symbol_result: Any) -> dict[str, Any]:
    display_bucket = _display(getattr(symbol_result, "display_bucket", NA)).lower()
    readiness_label = _display(getattr(symbol_result, "readiness_label", NA))
    if display_bucket == NA.lower() or readiness_label == NA:
        try:
            from app.formatters.scanner_display import build_symbol_display

            display = build_symbol_display(symbol_result)
            display_bucket = display.display_bucket
            readiness_label = display.readiness_label
        except Exception:
            display_bucket = _fallback_display_bucket(symbol_result)
            readiness_label = _fallback_readiness_label(symbol_result)

    timed_out = bool(getattr(symbol_result, "timed_out", False)) or _display(getattr(symbol_result, "timeout_status", "none")) != "none"
    error_message = _display(getattr(symbol_result, "error_message", NA))
    missing = _sequence_values(getattr(symbol_result, "missing_data", ()))
    strategy_missing = _sequence_values(getattr(symbol_result, "strategy_missing_data", ()))
    derivatives_missing = _sequence_values(getattr(symbol_result, "derivatives_missing_data", ()))
    unverified = _sequence_values(getattr(symbol_result, "unverified_data", ()))
    strategy_unverified = _sequence_values(getattr(symbol_result, "strategy_unverified_data", ()))
    derivatives_unverified = _sequence_values(getattr(symbol_result, "derivatives_unverified_data", ()))
    data_issue = (
        not timed_out
        and (
            display_bucket == "data_issue"
            or error_message != NA
            or _has_critical_data_issue(
                (
                    *missing,
                    *strategy_missing,
                    *derivatives_missing,
                    *unverified,
                    *strategy_unverified,
                    *derivatives_unverified,
                )
            )
        )
    )
    lifecycle_state = _lifecycle_state(symbol_result)
    lifecycle_progressed = bool(getattr(getattr(symbol_result, "lifecycle_transition", None), "transitioned", False))
    useful = (
        display_bucket in {"valid", "near_miss"}
        or readiness_label in HOT_READINESS_LABELS
        or lifecycle_state in PRIORITY_LIFECYCLE_STATES
        or lifecycle_state in WATCH_LIFECYCLE_STATES
        or lifecycle_progressed
    )
    return {
        "display_bucket": display_bucket if display_bucket else NA,
        "readiness_label": readiness_label if readiness_label else NA,
        "timed_out": timed_out,
        "data_issue": data_issue,
        "useful": useful,
        "lifecycle_state": lifecycle_state,
    }


def calculate_next_health_score(
    *,
    previous_score: int,
    timed_out: bool,
    data_issue: bool,
    useful: bool,
    no_setup: bool,
    lifecycle_state: str = NA,
    readiness_label: str = NA,
    timeout_strikes: int = 0,
) -> int:
    score = int(previous_score or DEFAULT_SYMBOL_HEALTH_SCORE)
    if timed_out:
        score -= 18 + min(18, max(0, timeout_strikes - 1) * 6)
    elif data_issue:
        score -= 8
    else:
        score += 4

    if useful:
        score += 6
    if _display(readiness_label) == "HOT WATCH":
        score += 4
    if lifecycle_state in {"TRIGGERED", "CONFIRMED"}:
        score += 8
    elif lifecycle_state in {"EXECUTING", "MANAGING"}:
        score += 10
    if no_setup and not useful:
        score -= 2
    return _bounded_int(score)


def update_average_runtime(
    previous_average: float,
    *,
    previous_count: int,
    runtime_sec: float | None,
) -> float:
    if runtime_sec is None:
        return round(max(float(previous_average or 0.0), 0.0), 3)
    count = max(0, int(previous_count))
    return round(((float(previous_average or 0.0) * count) + max(runtime_sec, 0.0)) / (count + 1), 3)


def build_symbol_health_summary(
    *,
    enabled: bool,
    plan: SymbolPriorityPlan | None,
    records: Mapping[str, SymbolHealthRecord],
    symbol_results: Sequence[Any],
) -> dict[str, Any]:
    priority_summary = plan.to_summary() if plan is not None else {"enabled": enabled}
    timeout_strikes_this_run = sum(1 for result in symbol_results if bool(getattr(result, "timed_out", False)))
    slowest = slow_symbol_payload(symbol_results)
    record_payload = {symbol: record.model_dump(mode="json") for symbol, record in sorted(records.items())}
    priority_summary.update(
        {
            "enabled": enabled,
            "timeout_strikes_this_run": timeout_strikes_this_run,
            "bad_behavior_events_this_run": len(tuple(symbol_health_events_from_results(symbol_results))),
            "slowest_symbols": slowest,
            "records": record_payload,
        }
    )
    priority_summary.setdefault("prioritized_symbols", len(symbol_results))
    priority_summary.setdefault("cooldown_symbols", 0)
    priority_summary.setdefault("skipped_due_to_cooldown", 0)
    priority_summary.setdefault("active_lifecycle_cooldown_exemptions", 0)
    priority_summary.setdefault("priority_symbols", [])
    return priority_summary


def slow_symbol_payload(symbol_results: Sequence[Any], *, limit: int = MAX_SLOW_SYMBOLS) -> list[dict[str, Any]]:
    runtimes = [
        (_symbol_from_result(result), _runtime_seconds(result))
        for result in symbol_results
        if _runtime_seconds(result) is not None
    ]
    runtimes = [(symbol, runtime) for symbol, runtime in runtimes if symbol != NA and runtime is not None]
    runtimes.sort(key=lambda item: item[1], reverse=True)
    return [
        {
            "symbol": symbol,
            "runtime_sec": round(runtime, 3),
        }
        for symbol, runtime in runtimes[: max(0, int(limit))]
    ]


def symbol_health_events_from_results(
    symbol_results: Sequence[Any],
    *,
    now: str | None = None,
) -> tuple[dict[str, Any], ...]:
    timestamp = now or now_utc_iso()
    events: list[dict[str, Any]] = []
    for result in symbol_results:
        events.extend(symbol_health_events_from_result(result, now=timestamp))
    return tuple(events)


def symbol_health_events_from_result(symbol_result: Any, *, now: str | None = None) -> tuple[dict[str, Any], ...]:
    timestamp = now or now_utc_iso()
    symbol = _symbol_from_result(symbol_result)
    if symbol == NA:
        return ()
    transition = getattr(symbol_result, "lifecycle_transition", None)
    record = getattr(symbol_result, "lifecycle_state", None)
    lifecycle_id = _display(getattr(record, "lifecycle_id", NA))
    if transition is None or not bool(getattr(transition, "transitioned", False)):
        return ()
    to_state = _display(getattr(getattr(transition, "to_state", None), "value", getattr(transition, "to_state", NA))).upper()
    from_state = _display(getattr(getattr(transition, "from_state", None), "value", getattr(transition, "from_state", NA))).upper()
    failed_gate = _display(getattr(record, "failed_gate", NA))
    details = {
        "from_state": from_state,
        "to_state": to_state,
        "failed_gate": failed_gate,
        "reason": _display(getattr(getattr(transition, "reason", None), "value", getattr(transition, "reason", NA))),
    }
    events: list[dict[str, Any]] = []
    if to_state == "INVALIDATED":
        events.append(_health_event(symbol, "invalidation", timestamp, lifecycle_id, details))
        if from_state in {"CONFIRMED", "EXECUTING", "MANAGING"}:
            events.append(_health_event(symbol, "false_confirmation", timestamp, lifecycle_id, details))
    elif to_state == "EXPIRED":
        events.append(_health_event(symbol, "expired_setup", timestamp, lifecycle_id, details))
    elif to_state == "REJECTED":
        events.append(_health_event(symbol, "rejected_setup", timestamp, lifecycle_id, details))
        if _status_key(failed_gate) in {"missing_entry", "missing_entry_zone", "missing_stop", "missing_invalidation"}:
            events.append(_health_event(symbol, "malformed_setup_event", timestamp, lifecycle_id, details))
    elif to_state == "SL_HIT" and from_state in {"CONFIRMED", "EXECUTING", "MANAGING"}:
        events.append(_health_event(symbol, "stop_breach_after_confirmation", timestamp, lifecycle_id, details))
    return tuple(events)


def cooldown_expiry(timestamp: str, cooldown_minutes: float) -> str:
    parsed = parse_timestamp(timestamp) or datetime.now(timezone.utc)
    return (parsed + timedelta(minutes=max(float(cooldown_minutes), 0.0))).replace(microsecond=0).isoformat()


def _health_event(
    symbol: str,
    event_type: str,
    occurred_at: str,
    lifecycle_id: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "lifecycle_id": lifecycle_id if lifecycle_id != NA else None,
        "details": dict(details),
    }


def _event_counts(events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = _display(event.get("event_type"))
        if event_type == NA:
            continue
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _event_health_penalty(counts: Mapping[str, int]) -> int:
    return (
        counts.get("invalidation", 0) * 5
        + counts.get("expired_setup", 0) * 3
        + counts.get("rejected_setup", 0) * 2
        + counts.get("false_confirmation", 0) * 8
        + counts.get("malformed_setup_event", 0) * 5
        + counts.get("stop_breach_after_confirmation", 0) * 8
        + counts.get("duplicate_noisy_setup", 0) * 3
    )


def cooldown_active(cooldown_until: str | None, now: str | None = None) -> bool:
    if cooldown_until in (None, "", NA):
        return False
    expiry = parse_timestamp(cooldown_until)
    current = parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
    if expiry is None or current is None:
        return False
    return current < expiry


def parse_timestamp(value: str | None) -> datetime | None:
    if value in (None, "", NA):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _priority_reasons(record: SymbolHealthRecord, lifecycle_state: str) -> tuple[str, ...]:
    reasons: list[str] = []
    if lifecycle_state in PRIORITY_LIFECYCLE_STATES:
        reasons.append(f"lifecycle_{lifecycle_state.lower()}")
    elif lifecycle_state in WATCH_LIFECYCLE_STATES:
        reasons.append(f"lifecycle_{lifecycle_state.lower()}")
    if record.last_readiness_label in HOT_READINESS_LABELS:
        reasons.append(record.last_readiness_label.lower().replace(" ", "_"))
    if record.last_display_bucket in NEAR_MISS_BUCKETS:
        reasons.append(record.last_display_bucket)
    if record.current_health_score >= 80:
        reasons.append("high_health")
    elif record.current_health_score < 40:
        reasons.append("low_health")
    if record.timeout_strikes:
        reasons.append("timeout_strikes")
    if record.data_issue_count:
        reasons.append("data_issues")
    if record.useful_scan_count:
        reasons.append("previously_useful")
    return tuple(dict.fromkeys(reasons))


def _lifecycle_priority_rank(state: str) -> int:
    if state in ACTIVE_LIFECYCLE_PRIORITY:
        return ACTIVE_LIFECYCLE_PRIORITY[state]
    return len(ACTIVE_LIFECYCLE_PRIORITY)


def _hot_watch_rank(record: SymbolHealthRecord) -> int:
    if record.last_readiness_label in HOT_READINESS_LABELS:
        return 0
    if record.last_display_bucket in NEAR_MISS_BUCKETS:
        return 0
    return 1


def _stale_rejected_rank(record: SymbolHealthRecord, now: str) -> int:
    if record.last_display_bucket not in LOW_VALUE_BUCKETS:
        return 0
    last_rejected = parse_timestamp(record.last_rejected_at or record.last_scanned_at)
    current = parse_timestamp(now)
    if last_rejected is None or current is None:
        return 1
    return 1 if current - last_rejected > timedelta(hours=24) else 0


def _fallback_display_bucket(symbol_result: Any) -> str:
    if getattr(symbol_result, "trade_idea", None) is not None or getattr(symbol_result, "valid_strategy_modes", ()):
        return "valid"
    if getattr(symbol_result, "error_message", None):
        return "data_issue"
    return "no_setup"


def _fallback_readiness_label(symbol_result: Any) -> str:
    if getattr(symbol_result, "trade_idea", None) is not None or getattr(symbol_result, "valid_strategy_modes", ()):
        return "VALID SETUP"
    if getattr(symbol_result, "error_message", None):
        return "DATA ISSUE"
    return "REJECTED"


def _has_critical_data_issue(values: Sequence[str]) -> bool:
    critical_prefixes = (
        "candles:",
        "candles_15m:",
        "candles_5m:",
        "execution_candles:",
        "confirmation_candles:",
        "current_price:",
        "latest_close:",
    )
    return any(str(value).startswith(critical_prefixes) for value in values)


def _lifecycle_state(symbol_result: Any) -> str:
    record = getattr(symbol_result, "lifecycle_state", None)
    state = getattr(record, "current_state", None)
    value = getattr(state, "value", state)
    text = _display(value)
    return text.upper() if text != NA else NA


def _runtime_seconds(symbol_result: Any) -> float | None:
    value = getattr(symbol_result, "runtime_seconds", None)
    if value in (None, "", NA):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(number, 0.0)


def _symbol_from_result(symbol_result: Any) -> str:
    return _normalize_symbol(_display(getattr(symbol_result, "symbol", NA)))


def _normalize_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for symbol in symbols:
        normalized = _normalize_symbol(symbol)
        if normalized != NA and normalized not in output:
            output.append(normalized)
    return tuple(output)


def _normalize_symbol(symbol: Any) -> str:
    text = _display(symbol).strip().upper()
    return text if text else NA


def _sequence_values(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(_display(value) for value in values if _display(value) != NA)


def _bounded_int(value: Any) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = DEFAULT_SYMBOL_HEALTH_SCORE
    return max(0, min(100, score))


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    try:
        if not isinstance(value, bool):
            decimal = Decimal(str(value))
            if decimal.is_finite() and str(value).strip() == str(decimal):
                return str(value)
    except (InvalidOperation, ValueError):
        pass
    return str(value)


def _status_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


__all__ = [
    "DEFAULT_MAX_TIMEOUT_STRIKES",
    "DEFAULT_SYMBOL_COOLDOWN_MINUTES",
    "DEFAULT_SYMBOL_HEALTH_SCORE",
    "SymbolHealthRecord",
    "SymbolPriorityDecision",
    "SymbolPriorityPlan",
    "apply_priority_to_health_record",
    "build_symbol_health_summary",
    "build_symbol_priority_plan",
    "calculate_next_health_score",
    "classify_symbol_result",
    "cooldown_active",
    "cooldown_expiry",
    "default_symbol_health",
    "empty_symbol_priority_plan",
    "now_utc_iso",
    "parse_timestamp",
    "slow_symbol_payload",
    "symbol_health_events_from_result",
    "symbol_health_events_from_results",
    "update_average_runtime",
    "update_symbol_health_record",
    "update_symbol_health_records",
]
