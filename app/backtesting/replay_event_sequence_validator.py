from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.backtesting.replay_validation_scaffold import (
    ReplayValidationResult,
    ReplayValidationTimeline,
    build_replay_validation_plan_from_files,
)

NA = "N/A"
REPLAY_EVENT_SEQUENCE_SCHEMA_VERSION = "historical_replay_event_sequence_v1"

REPLAY_EVENT_SEQUENCE_SAFETY_NOTE = (
    "Historical replay event sequence validation is read-only audit only. It does not mutate artifacts, "
    "scanner results, lifecycle states, performance memory, database records, alerts, trade ideas, "
    "strategy behavior, or setup gates; it does not call exchanges, send Telegram messages, create signals, "
    "place trades, run historical PnL, calculate win rate, claim expectancy, infer hidden transitions, "
    "or invent market data."
)

IssueSeverity = Literal["info", "warning", "error"]

KNOWN_SEQUENCE_STATUSES = {
    "ALERT_CREATED",
    "CANCELLED",
    "CLOSED",
    "CONFIRMED",
    "COOLDOWN",
    "EXECUTING",
    "INVALIDATED",
    "JOURNAL_ENTRY_CREATED",
    "REJECTED",
    "SCAN_ERROR",
    "SCANNED_NO_SETUP",
    "SL_HIT",
    "STALKING",
    "STOPPED",
    "TP_HIT",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "TRADE_IDEA_CREATED",
    "TRIGGERED",
    "WATCH",
}
STATUS_ALIASES = {
    "alert_created": "ALERT_CREATED",
    "alert_dry_run_created": "ALERT_CREATED",
    "alert_sent": "ALERT_CREATED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "closed": "CLOSED",
    "confirmed": "CONFIRMED",
    "cooldown": "COOLDOWN",
    "executing": "EXECUTING",
    "hot_watch": "WATCH",
    "idea_created": "TRADE_IDEA_CREATED",
    "invalidated": "INVALIDATED",
    "journal_created": "JOURNAL_ENTRY_CREATED",
    "journal_entry_created": "JOURNAL_ENTRY_CREATED",
    "no_setup": "SCANNED_NO_SETUP",
    "rejected": "REJECTED",
    "scan_error": "SCAN_ERROR",
    "scanned_no_setup": "SCANNED_NO_SETUP",
    "sl": "SL_HIT",
    "sl_hit": "SL_HIT",
    "stalking": "STALKING",
    "stopped": "STOPPED",
    "stop_loss_hit": "SL_HIT",
    "take_profit_hit": "TP_HIT",
    "tp": "TP_HIT",
    "tp_1_hit": "TP1_HIT",
    "tp_2_hit": "TP2_HIT",
    "tp_3_hit": "TP3_HIT",
    "tp_hit": "TP_HIT",
    "tp1": "TP1_HIT",
    "tp1_hit": "TP1_HIT",
    "tp2": "TP2_HIT",
    "tp2_hit": "TP2_HIT",
    "tp3": "TP3_HIT",
    "tp3_hit": "TP3_HIT",
    "trade_idea_created": "TRADE_IDEA_CREATED",
    "triggered": "TRIGGERED",
    "valid_setup": "CONFIRMED",
    "watch": "WATCH",
    "watching": "WATCH",
    "watchlist": "WATCH",
    "watchlisted": "WATCH",
}

CREATED_STATUSES = {"TRADE_IDEA_CREATED", "ALERT_CREATED", "JOURNAL_ENTRY_CREATED"}
NEGATIVE_STATUSES = {"REJECTED", "SCANNED_NO_SETUP", "SCAN_ERROR"}
ACTIVE_EXECUTION_STATUSES = {"EXECUTING"}
ACTIVE_CONTEXT_STATUSES = {"TRIGGERED", "CONFIRMED", "EXECUTING"}
TP_STATUSES = {"TP_HIT", "TP1_HIT", "TP2_HIT", "TP3_HIT"}
SL_STATUSES = {"SL_HIT", "STOPPED"}
TERMINAL_STATUSES = TP_STATUSES | SL_STATUSES | {"INVALIDATED", "COOLDOWN", "CANCELLED", "CLOSED"}
TERMINAL_OR_NEGATIVE_STATUSES = TERMINAL_STATUSES | NEGATIVE_STATUSES
TRADE_LIKE_STATUSES = ACTIVE_CONTEXT_STATUSES | TP_STATUSES | SL_STATUSES | {
    "ALERT_CREATED",
    "CLOSED",
    "CONFIRMED",
    "INVALIDATED",
    "JOURNAL_ENTRY_CREATED",
    "TRADE_IDEA_CREATED",
}

ALLOWED_TRANSITIONS = {
    "WATCH": {"WATCH", "STALKING", "TRADE_IDEA_CREATED", "ALERT_CREATED", "REJECTED", "CANCELLED", "INVALIDATED"},
    "STALKING": {
        "WATCH",
        "STALKING",
        "TRIGGERED",
        "TRADE_IDEA_CREATED",
        "ALERT_CREATED",
        "REJECTED",
        "CANCELLED",
        "INVALIDATED",
    },
    "TRIGGERED": {
        "TRIGGERED",
        "CONFIRMED",
        "EXECUTING",
        "TRADE_IDEA_CREATED",
        "ALERT_CREATED",
        "JOURNAL_ENTRY_CREATED",
        "INVALIDATED",
        "CANCELLED",
        "STOPPED",
        "SL_HIT",
    },
    "CONFIRMED": {
        "CONFIRMED",
        "EXECUTING",
        "ALERT_CREATED",
        "JOURNAL_ENTRY_CREATED",
        "INVALIDATED",
        "CANCELLED",
        "CLOSED",
        "STOPPED",
        "SL_HIT",
    },
    "EXECUTING": {
        "EXECUTING",
        "TP_HIT",
        "TP1_HIT",
        "TP2_HIT",
        "TP3_HIT",
        "SL_HIT",
        "STOPPED",
        "INVALIDATED",
        "COOLDOWN",
        "CLOSED",
        "JOURNAL_ENTRY_CREATED",
    },
    "TRADE_IDEA_CREATED": {"WATCH", "STALKING", "TRIGGERED", "CONFIRMED", "ALERT_CREATED", "JOURNAL_ENTRY_CREATED"},
    "ALERT_CREATED": {"TRIGGERED", "CONFIRMED", "EXECUTING", "JOURNAL_ENTRY_CREATED", "CLOSED"},
    "JOURNAL_ENTRY_CREATED": {"JOURNAL_ENTRY_CREATED", "CLOSED", "COOLDOWN"},
    "TP_HIT": {"COOLDOWN", "CLOSED", "JOURNAL_ENTRY_CREATED"},
    "TP1_HIT": {"TP2_HIT", "TP3_HIT", "COOLDOWN", "CLOSED", "JOURNAL_ENTRY_CREATED"},
    "TP2_HIT": {"TP3_HIT", "COOLDOWN", "CLOSED", "JOURNAL_ENTRY_CREATED"},
    "TP3_HIT": {"COOLDOWN", "CLOSED", "JOURNAL_ENTRY_CREATED"},
    "SL_HIT": {"COOLDOWN", "CLOSED", "JOURNAL_ENTRY_CREATED"},
    "STOPPED": {"COOLDOWN", "CLOSED", "JOURNAL_ENTRY_CREATED"},
    "INVALIDATED": {"COOLDOWN", "CLOSED", "JOURNAL_ENTRY_CREATED"},
    "COOLDOWN": {"CLOSED"},
    "CANCELLED": {"CLOSED", "COOLDOWN"},
    "CLOSED": {"COOLDOWN", "JOURNAL_ENTRY_CREATED"},
    "REJECTED": {"CANCELLED", "CLOSED", "JOURNAL_ENTRY_CREATED"},
    "SCANNED_NO_SETUP": {"REJECTED", "JOURNAL_ENTRY_CREATED"},
    "SCAN_ERROR": {"JOURNAL_ENTRY_CREATED"},
}

SEVERE_READINESS_CODES = {
    "closed_to_executing",
    "cooldown_to_executing",
    "duplicate_event_identity",
    "invalidated_to_executing",
    "negative_to_executing",
    "rejected_to_executing",
    "scanned_no_setup_to_executing",
    "suspicious_transition",
    "terminal_followed_by_executing",
    "timestamp_decrease",
    "watch_to_sl_without_active_context",
    "watch_to_tp_without_active_context",
}
SUSPICIOUS_ISSUE_CODES = SEVERE_READINESS_CODES | {
    "alert_without_trade_idea_context",
    "journal_without_trade_context",
}
TIMESTAMP_FIELDS = (
    "timestamp",
    "scan_timestamp",
    "created_at",
    "updated_at",
    "scanned_at",
    "seen_at",
    "first_seen_at",
    "last_seen_at",
    "last_transition_at",
    "completed_at",
    "started_at",
    "run_timestamp",
)
STATUS_FIELDS = ("normalized_lifecycle_status", "status", "lifecycle_status", "current_state", "state", "to_state")
SYMBOL_FIELDS = ("symbol", "ticker", "market")
STRATEGY_MODE_FIELDS = ("strategy_mode", "setup_mode", "mode")
DIRECTION_FIELDS = ("direction", "side", "bias")
EVENT_IDENTITY_FIELDS = ("event_id", "event_key", "transition_id", "row_id", "candidate_id", "journal_entry_id")


@dataclass(frozen=True)
class ReplaySequenceIssue:
    severity: IssueSeverity
    code: str
    message: str
    group_key: str = "root"
    path: str = "root"


@dataclass(frozen=True)
class ReplaySequenceTransition:
    group_key: str
    from_event_index: int
    to_event_index: int
    from_status: str
    to_status: str
    transition_key: str
    suspicious: bool = False
    issue_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplaySequenceGroup:
    group_key: str
    symbol: str = NA
    strategy_mode: str = NA
    direction: str = NA
    event_count: int = 0
    ordered_events: tuple[dict[str, Any], ...] = ()
    transitions: tuple[ReplaySequenceTransition, ...] = ()
    issue_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    ordering_method: str = "input_order"
    has_terminal_event: bool = False
    has_trade_like_event: bool = False
    has_negative_example_event: bool = False
    sequence_ready: bool = False
    sequence_readiness_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplaySequenceValidationSummary:
    schema_version: str = REPLAY_EVENT_SEQUENCE_SCHEMA_VERSION
    source: str = "in_memory"
    total_events: int = 0
    total_groups: int = 0
    sequence_ready_groups: int = 0
    sequence_not_ready_groups: int = 0
    sequence_ready_rate: float = 0.0
    negative_example_groups: int = 0
    trade_like_groups: int = 0
    terminal_groups: int = 0
    unknown_identity_groups: int = 0
    groups_missing_timestamps: int = 0
    groups_with_suspicious_transitions: int = 0
    duplicate_event_count: int = 0
    timestamp_order_issue_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    top_issue_codes: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)
    event_type_counts: dict[str, int] = field(default_factory=dict)
    transition_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplaySequenceValidationResult:
    source: str
    schema_version: str = REPLAY_EVENT_SEQUENCE_SCHEMA_VERSION
    summary: ReplaySequenceValidationSummary = field(default_factory=ReplaySequenceValidationSummary)
    groups: tuple[ReplaySequenceGroup, ...] = ()
    issues: tuple[ReplaySequenceIssue, ...] = ()
    safety_note: str = REPLAY_EVENT_SEQUENCE_SAFETY_NOTE


def normalize_sequence_status(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return NA
    key = _status_key(text)
    if key == "unknown":
        return "UNKNOWN"
    alias = STATUS_ALIASES.get(key)
    if alias is not None:
        return alias
    upper = key.upper()
    if upper in KNOWN_SEQUENCE_STATUSES:
        return upper
    return upper


def classify_sequence_event_type(event_or_candidate: Any) -> str:
    data = _row_to_dict(event_or_candidate)
    if data is None:
        return "UNKNOWN_EVENT"

    explicit = _event_type_alias(data.get("event_type"))
    status = _event_status(data)
    if explicit != "UNKNOWN_EVENT":
        return explicit
    if status in CREATED_STATUSES:
        return status
    if status in NEGATIVE_STATUSES:
        return "NEGATIVE_EXAMPLE"
    if status in TERMINAL_STATUSES:
        return "TERMINAL_OUTCOME"
    if _has_trade_context(data) or status in TRADE_LIKE_STATUSES:
        return "TRADE_LIKE_EVENT"
    if status in KNOWN_SEQUENCE_STATUSES:
        return "LIFECYCLE_EVENT"
    return "UNKNOWN_EVENT"


def validate_replay_event_sequence(
    validation_result_or_events: Any,
    source: str = "in_memory",
) -> ReplaySequenceValidationResult:
    event_dicts, input_issues, result_source = _extract_input_events(validation_result_or_events, source=source)
    normalized_events = tuple(_normalize_event(event, index) for index, event in enumerate(event_dicts))
    groups, group_issues = _build_sequence_groups(normalized_events)
    issues = tuple(input_issues) + tuple(group_issues)
    summary = _build_summary(source=result_source, events=normalized_events, groups=groups, issues=issues)
    return ReplaySequenceValidationResult(
        source=result_source,
        summary=summary,
        groups=groups,
        issues=issues,
    )


def validate_replay_event_sequence_from_files(paths: list[Path]) -> ReplaySequenceValidationResult:
    try:
        validation_result = build_replay_validation_plan_from_files(paths)
    except Exception as exc:  # pragma: no cover - defensive for unexpected filesystem errors.
        issue = ReplaySequenceIssue(
            severity="error",
            code="input_file_error",
            message=f"Replay event sequence input could not be read: {exc}",
        )
        return _result_from_issues(source="files", issues=(issue,))
    return validate_replay_event_sequence(validation_result, source=validation_result.source)


def replay_sequence_validation_result_to_dict(result: ReplaySequenceValidationResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def _extract_input_events(value: Any, *, source: str) -> tuple[tuple[dict[str, Any], ...], tuple[ReplaySequenceIssue, ...], str]:
    if isinstance(value, ReplayValidationResult):
        issues = tuple(_issue_from_validation_issue(issue) for issue in value.issues if _issue_severity(issue) != "info")
        if value.candidates:
            return _events_from_sequence(value.candidates, issues), issues, _first_non_na(value.source, source)
        return _events_from_sequence(value.timeline.ordered_events, issues), issues, _first_non_na(value.source, source)

    if isinstance(value, ReplayValidationTimeline):
        return _events_from_sequence(value.ordered_events, ()), (), _first_non_na(value.source, source)

    data = _row_to_dict(value)
    if data is not None and not _looks_like_single_event(data):
        for field_name in ("ordered_events", "events", "candidates", "results"):
            if field_name not in data:
                continue
            collection = data.get(field_name)
            if not _is_sequence(collection):
                issue = ReplaySequenceIssue(
                    severity="error",
                    code="event_collection_wrong_type",
                    message=f"{field_name} must be a sequence of replay events for sequence validation.",
                    path=field_name,
                )
                return (), (issue,), source
            return _events_from_sequence(collection, ()), (), _first_non_na(_text(data.get("source")), source)

    if _is_sequence(value):
        return _events_from_sequence(value, ()), (), source

    if data is not None:
        return _events_from_sequence((data,), ()), (), _first_non_na(_text(data.get("source")), source)

    issue = ReplaySequenceIssue(
        severity="error",
        code="invalid_event_sequence_input",
        message="Replay event sequence input must be a validation result, timeline, mapping, or sequence of events.",
    )
    return (), (issue,), source


def _events_from_sequence(
    values: Sequence[Any],
    _existing_issues: Sequence[ReplaySequenceIssue],
) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        data = _row_to_dict(value)
        if data is None:
            continue
        events.extend(_expand_status_history(data, input_index=index))
    return tuple(events)


def _expand_status_history(data: Mapping[str, Any], *, input_index: int) -> tuple[dict[str, Any], ...]:
    if "status_history" not in data:
        event = dict(data)
        event.setdefault("row_index", input_index)
        return (event,)

    history = data.get("status_history")
    if history in (None, "", NA) or history == ():
        event = dict(data)
        event.setdefault("row_index", input_index)
        return (event,)

    if not _is_sequence(history):
        event = dict(data)
        event.setdefault("row_index", input_index)
        event["_sequence_input_error"] = "status_history_wrong_type"
        return (event,)

    if len(history) == 0:
        event = dict(data)
        event.setdefault("row_index", input_index)
        return (event,)

    events: list[dict[str, Any]] = []
    for history_index, item in enumerate(history):
        event = dict(data)
        event["row_index"] = data.get("row_index", input_index)
        event["status_history_index"] = history_index
        if isinstance(item, Mapping):
            event.update({str(key): _jsonable(value) for key, value in item.items()})
        else:
            event["status"] = item
            event["normalized_lifecycle_status"] = item
        events.append(event)
    return tuple(events)


def _normalize_event(data: Mapping[str, Any], input_index: int) -> dict[str, Any]:
    normalized = {str(key): _jsonable(value) for key, value in data.items()}
    status = _event_status(normalized)
    event_type = classify_sequence_event_type(normalized)
    group_key, identity_kind = _sequence_group_key(normalized)
    timestamp = _first_text(normalized, TIMESTAMP_FIELDS)
    event_identity = _event_identity(normalized)
    history_index = _text(normalized.get("status_history_index"))
    if event_identity != NA and history_index != NA:
        event_identity = f"{event_identity}:history:{history_index}"

    return {
        "input_index": input_index,
        "row_index": _text(normalized.get("row_index")),
        "group_key": group_key,
        "identity_kind": identity_kind,
        "event_identity": event_identity,
        "symbol": _uppercase(_first_text(normalized, SYMBOL_FIELDS)),
        "strategy_mode": _first_text(normalized, STRATEGY_MODE_FIELDS),
        "direction": _lowercase(_first_text(normalized, DIRECTION_FIELDS)),
        "timestamp": timestamp,
        "status": status,
        "event_type": event_type,
        "has_trade_context": _has_trade_context(normalized),
        "has_alert_context": _has_alert_context(normalized),
        "has_journal_context": _has_journal_context(normalized),
        "sequence_input_error": _text(normalized.get("_sequence_input_error")),
    }


def _build_sequence_groups(events: Sequence[Mapping[str, Any]]) -> tuple[tuple[ReplaySequenceGroup, ...], tuple[ReplaySequenceIssue, ...]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    group_order: list[str] = []
    for event in events:
        group_key = _text(event.get("group_key"))
        if group_key not in grouped:
            group_order.append(group_key)
        grouped[group_key].append(event)

    groups: list[ReplaySequenceGroup] = []
    issues: list[ReplaySequenceIssue] = []
    for group_key in group_order:
        group, group_issues = _build_group(group_key, grouped[group_key])
        groups.append(group)
        issues.extend(group_issues)
    return tuple(groups), tuple(issues)


def _build_group(group_key: str, events: Sequence[Mapping[str, Any]]) -> tuple[ReplaySequenceGroup, tuple[ReplaySequenceIssue, ...]]:
    group_issues: list[ReplaySequenceIssue] = []
    ordered_events = tuple(_public_event(event) for event in events)
    statuses = tuple(_text(event.get("status")) for event in events)
    event_types = tuple(_text(event.get("event_type")) for event in events)
    timestamps = tuple(_text(event.get("timestamp")) for event in events)

    if group_key == "unknown_identity":
        _add_issue(group_issues, "warning", "missing_identity", "Replay sequence group has no stable identity.", group_key)

    symbol = _first_present_text(event.get("symbol") for event in events)
    if symbol == NA:
        _add_issue(group_issues, "warning", "missing_symbol", "Replay sequence group is missing symbol in all events.", group_key)

    if not any(status in KNOWN_SEQUENCE_STATUSES for status in statuses) and not any(
        event_type != "UNKNOWN_EVENT" for event_type in event_types
    ):
        _add_issue(
            group_issues,
            "warning",
            "missing_status_or_event_type",
            "Replay sequence group has no known status or event type.",
            group_key,
        )

    for event in events:
        _audit_event_status(event, group_issues)
        if _text(event.get("sequence_input_error")) == "status_history_wrong_type":
            _add_issue(
                group_issues,
                "error",
                "status_history_wrong_type",
                "status_history must be a sequence when present for sequence validation.",
                group_key,
                path=f"events[{event.get('input_index')}].status_history",
            )

    duplicate_count = _audit_duplicate_event_identities(events, group_issues, group_key)
    timestamp_issue_count, ordering_method = _audit_timestamps(events, group_issues, group_key)
    transitions = _audit_transitions(events, group_issues, group_key)
    _audit_context_requirements(events, group_issues, group_key)

    warning_count = sum(1 for issue in group_issues if issue.severity == "warning")
    error_count = sum(1 for issue in group_issues if issue.severity == "error")
    issue_count = warning_count + error_count
    readiness_warnings = tuple(issue.message for issue in group_issues if issue.severity in {"warning", "error"})
    severe_issue_present = any(issue.code in SEVERE_READINESS_CODES for issue in group_issues)
    has_terminal = any(status in TERMINAL_OR_NEGATIVE_STATUSES for status in statuses)
    has_negative = any(status in NEGATIVE_STATUSES for status in statuses)
    has_trade_like = any(
        status in TRADE_LIKE_STATUSES
        or event_type in {"TRADE_LIKE_EVENT", "TERMINAL_OUTCOME", "TRADE_IDEA_CREATED", "ALERT_CREATED", "JOURNAL_ENTRY_CREATED"}
        or bool(event.get("has_trade_context"))
        for status, event_type, event in zip(statuses, event_types, events)
    )
    has_known_status_or_event = any(status in KNOWN_SEQUENCE_STATUSES for status in statuses) or any(
        event_type != "UNKNOWN_EVENT" for event_type in event_types
    )
    sequence_ready = (
        group_key != "unknown_identity"
        and symbol != NA
        and has_known_status_or_event
        and ordering_method != "unavailable"
        and error_count == 0
        and not severe_issue_present
    )

    group = ReplaySequenceGroup(
        group_key=group_key,
        symbol=symbol,
        strategy_mode=_first_present_text(event.get("strategy_mode") for event in events),
        direction=_first_present_text(event.get("direction") for event in events),
        event_count=len(events),
        ordered_events=ordered_events,
        transitions=transitions,
        issue_count=issue_count,
        warning_count=warning_count,
        error_count=error_count,
        ordering_method=ordering_method,
        has_terminal_event=has_terminal,
        has_trade_like_event=has_trade_like,
        has_negative_example_event=has_negative,
        sequence_ready=sequence_ready,
        sequence_readiness_warnings=readiness_warnings,
    )
    if duplicate_count or timestamp_issue_count:
        return group, tuple(group_issues)
    return group, tuple(group_issues)


def _audit_event_status(event: Mapping[str, Any], issues: list[ReplaySequenceIssue]) -> None:
    status = _text(event.get("status"))
    event_type = _text(event.get("event_type"))
    group_key = _text(event.get("group_key"))
    input_index = _text(event.get("input_index"))

    if status == NA:
        _add_issue(
            issues,
            "warning",
            "missing_status",
            "Replay sequence event is missing status.",
            group_key,
            path=f"events[{input_index}].status",
        )
        return
    if status not in KNOWN_SEQUENCE_STATUSES:
        _add_issue(
            issues,
            "warning",
            "unknown_status",
            f"Replay sequence event has unknown status {status}.",
            group_key,
            path=f"events[{input_index}].status",
        )
    if event_type == "UNKNOWN_EVENT":
        _add_issue(
            issues,
            "warning",
            "unknown_event_type",
            "Replay sequence event type could not be classified.",
            group_key,
            path=f"events[{input_index}].event_type",
        )


def _audit_duplicate_event_identities(
    events: Sequence[Mapping[str, Any]],
    issues: list[ReplaySequenceIssue],
    group_key: str,
) -> int:
    seen: set[str] = set()
    duplicates = 0
    for event in events:
        identity = _text(event.get("event_identity"))
        if identity == NA:
            continue
        if identity in seen:
            duplicates += 1
            _add_issue(
                issues,
                "warning",
                "duplicate_event_identity",
                f"Duplicate replay event identity {identity} appears in the same sequence group.",
                group_key,
                path=f"events[{event.get('input_index')}].event_identity",
            )
        seen.add(identity)
    return duplicates


def _audit_timestamps(
    events: Sequence[Mapping[str, Any]],
    issues: list[ReplaySequenceIssue],
    group_key: str,
) -> tuple[int, str]:
    if not events:
        return 0, "unavailable"

    timestamps = tuple(_text(event.get("timestamp")) for event in events)
    if all(timestamp == NA for timestamp in timestamps):
        _add_issue(
            issues,
            "warning",
            "missing_timestamps",
            "Replay sequence group is missing timestamps in all events; input order fallback was used.",
            group_key,
        )
        return 0, "input_order_missing_timestamps"
    if any(timestamp == NA for timestamp in timestamps):
        _add_issue(
            issues,
            "warning",
            "partial_missing_timestamps",
            "Replay sequence group has one or more missing timestamps; input order fallback was used.",
            group_key,
        )
        return 0, "input_order_partial_missing_timestamps"

    issue_count = 0
    previous_key: tuple[int, Any] | None = None
    for event in events:
        key = _timestamp_key(_text(event.get("timestamp")))
        if previous_key is not None and key < previous_key:
            issue_count += 1
            _add_issue(
                issues,
                "warning",
                "timestamp_decrease",
                "Replay sequence timestamp decreases within the group.",
                group_key,
                path=f"events[{event.get('input_index')}].timestamp",
            )
        previous_key = key
    if issue_count:
        return issue_count, "input_order_timestamp_decrease"
    return 0, "timestamp_order"


def _audit_transitions(
    events: Sequence[Mapping[str, Any]],
    issues: list[ReplaySequenceIssue],
    group_key: str,
) -> tuple[ReplaySequenceTransition, ...]:
    transitions: list[ReplaySequenceTransition] = []
    statuses = tuple(_text(event.get("status")) for event in events)
    for transition_index, (previous_event, current_event) in enumerate(zip(events, events[1:]), start=1):
        previous = _text(previous_event.get("status"))
        current = _text(current_event.get("status"))
        transition_codes = _transition_issue_codes(statuses[:transition_index], previous, current)
        for code in transition_codes:
            _add_issue(
                issues,
                "warning",
                code,
                _transition_message(code, previous, current),
                group_key,
                path=f"events[{current_event.get('input_index')}].status",
            )
        transitions.append(
            ReplaySequenceTransition(
                group_key=group_key,
                from_event_index=int(previous_event.get("input_index", 0)),
                to_event_index=int(current_event.get("input_index", 0)),
                from_status=previous,
                to_status=current,
                transition_key=f"{previous}->{current}",
                suspicious=bool(transition_codes),
                issue_codes=tuple(transition_codes),
            )
        )
    return tuple(transitions)


def _transition_issue_codes(prior_statuses: Sequence[str], previous: str, current: str) -> list[str]:
    if previous == NA or current == NA or previous == current:
        return []
    if previous not in KNOWN_SEQUENCE_STATUSES or current not in KNOWN_SEQUENCE_STATUSES:
        return []

    codes: list[str] = []
    prior_status_set = set(prior_statuses)
    if current in TP_STATUSES and "WATCH" in prior_status_set and not prior_status_set & ACTIVE_CONTEXT_STATUSES:
        codes.append("watch_to_tp_without_active_context")
    if current in SL_STATUSES and "WATCH" in prior_status_set and not prior_status_set & ACTIVE_CONTEXT_STATUSES:
        codes.append("watch_to_sl_without_active_context")
    if previous in {"REJECTED", "SCANNED_NO_SETUP", "SCAN_ERROR"} and current == "EXECUTING":
        if previous == "REJECTED":
            codes.append("rejected_to_executing")
        elif previous == "SCANNED_NO_SETUP":
            codes.append("scanned_no_setup_to_executing")
        else:
            codes.append("negative_to_executing")
    if previous == "INVALIDATED" and current == "EXECUTING":
        codes.append("invalidated_to_executing")
    if previous == "COOLDOWN" and current == "EXECUTING":
        codes.append("cooldown_to_executing")
    if previous == "CLOSED" and current == "EXECUTING":
        codes.append("closed_to_executing")
    if previous in TERMINAL_OR_NEGATIVE_STATUSES and current in ACTIVE_EXECUTION_STATUSES:
        codes.append("terminal_followed_by_executing")

    allowed = ALLOWED_TRANSITIONS.get(previous)
    if allowed is not None and current not in allowed and not any(code.endswith("_to_executing") for code in codes):
        codes.append("suspicious_transition")
    return _unique_strings(codes)


def _audit_context_requirements(
    events: Sequence[Mapping[str, Any]],
    issues: list[ReplaySequenceIssue],
    group_key: str,
) -> None:
    has_trade_idea_context = any(
        _text(event.get("status")) == "TRADE_IDEA_CREATED" or bool(event.get("has_trade_context")) for event in events
    )
    has_alert_context = any(_text(event.get("status")) == "ALERT_CREATED" or bool(event.get("has_alert_context")) for event in events)
    for event in events:
        status = _text(event.get("status"))
        if status == "ALERT_CREATED" and not has_trade_idea_context:
            _add_issue(
                issues,
                "warning",
                "alert_without_trade_idea_context",
                "ALERT_CREATED appears without trade idea context in the same sequence group.",
                group_key,
                path=f"events[{event.get('input_index')}].status",
            )
        if status == "JOURNAL_ENTRY_CREATED" and not (has_trade_idea_context or has_alert_context):
            _add_issue(
                issues,
                "warning",
                "journal_without_trade_context",
                "JOURNAL_ENTRY_CREATED appears without trade idea or alert context in the same sequence group.",
                group_key,
                path=f"events[{event.get('input_index')}].status",
            )


def _build_summary(
    *,
    source: str,
    events: Sequence[Mapping[str, Any]],
    groups: Sequence[ReplaySequenceGroup],
    issues: Sequence[ReplaySequenceIssue],
) -> ReplaySequenceValidationSummary:
    warning_count = sum(1 for issue in issues if issue.severity == "warning")
    error_count = sum(1 for issue in issues if issue.severity == "error")
    issue_counts = Counter(issue.code for issue in issues if issue.severity in {"warning", "error"})
    status_counts = Counter(_text(event.get("status")) for event in events)
    event_type_counts = Counter(_text(event.get("event_type")) for event in events)
    transition_counts = Counter(transition.transition_key for group in groups for transition in group.transitions)
    ready_count = sum(1 for group in groups if group.sequence_ready)

    return ReplaySequenceValidationSummary(
        source=source,
        total_events=len(events),
        total_groups=len(groups),
        sequence_ready_groups=ready_count,
        sequence_not_ready_groups=max(0, len(groups) - ready_count),
        sequence_ready_rate=_rate(ready_count, len(groups)),
        negative_example_groups=sum(1 for group in groups if group.has_negative_example_event),
        trade_like_groups=sum(1 for group in groups if group.has_trade_like_event),
        terminal_groups=sum(1 for group in groups if group.has_terminal_event),
        unknown_identity_groups=sum(1 for group in groups if group.group_key == "unknown_identity"),
        groups_missing_timestamps=sum(1 for group in groups if group.ordering_method == "input_order_missing_timestamps"),
        groups_with_suspicious_transitions=sum(
            1
            for group in groups
            if any(
                code in SUSPICIOUS_ISSUE_CODES
                for transition in group.transitions
                for code in transition.issue_codes
            )
            or any(_message_matches_group_issue(issue, group.group_key, SUSPICIOUS_ISSUE_CODES) for issue in issues)
        ),
        duplicate_event_count=issue_counts["duplicate_event_identity"],
        timestamp_order_issue_count=issue_counts["timestamp_decrease"],
        warning_count=warning_count,
        error_count=error_count,
        top_issue_codes=dict(sorted(issue_counts.items(), key=lambda item: (-item[1], item[0]))[:10]),
        status_counts=dict(sorted(status_counts.items(), key=lambda item: (-item[1], item[0]))),
        event_type_counts=dict(sorted(event_type_counts.items(), key=lambda item: (-item[1], item[0]))),
        transition_counts=dict(sorted(transition_counts.items(), key=lambda item: (-item[1], item[0]))),
    )


def _message_matches_group_issue(issue: ReplaySequenceIssue, group_key: str, codes: set[str]) -> bool:
    return issue.group_key == group_key and issue.code in codes


def _result_from_issues(source: str, issues: Sequence[ReplaySequenceIssue]) -> ReplaySequenceValidationResult:
    summary = _build_summary(source=source, events=(), groups=(), issues=tuple(issues))
    return ReplaySequenceValidationResult(source=source, summary=summary, issues=tuple(issues))


def _issue_from_validation_issue(issue: Any) -> ReplaySequenceIssue:
    return ReplaySequenceIssue(
        severity=_issue_severity(issue),
        code=_text(getattr(issue, "code", "input_validation_issue")),
        message=_text(getattr(issue, "message", issue)),
        path=_text(getattr(issue, "path", "input")),
    )


def _issue_severity(issue: Any) -> IssueSeverity:
    severity = _text(getattr(issue, "severity", "warning")).lower()
    if severity in {"info", "warning", "error"}:
        return severity  # type: ignore[return-value]
    return "warning"


def _public_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_index": int(event.get("input_index", 0)),
        "row_index": _text(event.get("row_index")),
        "event_identity": _text(event.get("event_identity")),
        "timestamp": _text(event.get("timestamp")),
        "status": _text(event.get("status")),
        "event_type": _text(event.get("event_type")),
        "symbol": _text(event.get("symbol")),
        "strategy_mode": _text(event.get("strategy_mode")),
        "direction": _text(event.get("direction")),
    }


def _event_status(data: Mapping[str, Any]) -> str:
    for field_name in STATUS_FIELDS:
        status = normalize_sequence_status(data.get(field_name))
        if status != NA:
            return status
    event_type_status = normalize_sequence_status(data.get("event_type"))
    if event_type_status in CREATED_STATUSES | NEGATIVE_STATUSES | TERMINAL_STATUSES:
        return event_type_status
    return NA


def _event_type_alias(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return "UNKNOWN_EVENT"
    key = _status_key(text)
    aliases = {
        "alert": "ALERT_CREATED",
        "alert_created": "ALERT_CREATED",
        "alerted": "ALERT_CREATED",
        "journal": "JOURNAL_ENTRY_CREATED",
        "journaled": "JOURNAL_ENTRY_CREATED",
        "journal_entry_created": "JOURNAL_ENTRY_CREATED",
        "negative_example": "NEGATIVE_EXAMPLE",
        "terminal_outcome": "TERMINAL_OUTCOME",
        "trade_idea": "TRADE_IDEA_CREATED",
        "trade_idea_created": "TRADE_IDEA_CREATED",
        "trade_like_candidate": "TRADE_LIKE_EVENT",
        "trade_like_event": "TRADE_LIKE_EVENT",
    }
    if key in aliases:
        return aliases[key]
    normalized = normalize_sequence_status(value)
    if normalized in CREATED_STATUSES:
        return normalized
    return "UNKNOWN_EVENT"


def _sequence_group_key(data: Mapping[str, Any]) -> tuple[str, str]:
    for field_name in ("setup_id", "trade_idea_id", "alert_id"):
        value = _text(data.get(field_name))
        if value != NA:
            return f"{field_name}:{value}", field_name

    symbol = _uppercase(_first_text(data, SYMBOL_FIELDS))
    strategy_mode = _first_text(data, STRATEGY_MODE_FIELDS)
    run_id = _first_non_na(_text(data.get("run_id")), _text(data.get("scan_run_id")))
    if run_id != NA and symbol != NA and strategy_mode != NA:
        return f"run_symbol_mode:{run_id}|{symbol}|{strategy_mode}", "run_id_symbol_strategy_mode"

    scan_id = _text(data.get("scan_id"))
    if scan_id != NA and symbol != NA and strategy_mode != NA:
        return f"scan_symbol_mode:{scan_id}|{symbol}|{strategy_mode}", "scan_id_symbol_strategy_mode"

    candidate_id = _text(data.get("candidate_id"))
    if candidate_id != NA:
        return f"candidate_id:{candidate_id}", "candidate_id"

    row_index = _text(data.get("row_index"))
    if symbol != NA and row_index != NA:
        return f"symbol_row:{symbol}|{row_index}", "symbol_row_index"

    return "unknown_identity", "unknown_identity"


def _event_identity(data: Mapping[str, Any]) -> str:
    for field_name in EVENT_IDENTITY_FIELDS:
        value = _text(data.get(field_name))
        if value != NA:
            return f"{field_name}:{value}"
    return NA


def _has_trade_context(data: Mapping[str, Any]) -> bool:
    if _truthy(data.get("trade_idea_present")) or _text(data.get("trade_idea_id")) != NA:
        return True
    if _present(data.get("trade_idea")):
        return True
    return any(
        _text(data.get(field_name)) != NA
        for field_name in ("entry", "entry_low", "entry_high", "stop", "invalidation", "tp1", "tp2", "tp3")
    )


def _has_alert_context(data: Mapping[str, Any]) -> bool:
    return _truthy(data.get("alert_present")) or _text(data.get("alert_id")) != NA or _present(data.get("alert_result"))


def _has_journal_context(data: Mapping[str, Any]) -> bool:
    return (
        _truthy(data.get("journal_entry_present"))
        or _text(data.get("journal_entry_id")) != NA
        or _present(data.get("journal_entry"))
    )


def _looks_like_single_event(data: Mapping[str, Any]) -> bool:
    keys = set(data)
    return bool(
        keys
        & (
            set(STATUS_FIELDS)
            | set(SYMBOL_FIELDS)
            | {"candidate_id", "setup_id", "trade_idea_id", "alert_id", "event_type", "status_history"}
        )
    )


def _add_issue(
    issues: list[ReplaySequenceIssue],
    severity: IssueSeverity,
    code: str,
    message: str,
    group_key: str,
    path: str = "root",
) -> None:
    issues.append(ReplaySequenceIssue(severity=severity, code=code, message=message, group_key=group_key, path=path))


def _transition_message(code: str, previous: str, current: str) -> str:
    messages = {
        "closed_to_executing": "CLOSED is followed by EXECUTING in the same sequence group.",
        "cooldown_to_executing": "COOLDOWN is followed by EXECUTING in the same sequence group.",
        "invalidated_to_executing": "INVALIDATED is followed by EXECUTING in the same sequence group.",
        "negative_to_executing": "A negative terminal status is followed by EXECUTING in the same sequence group.",
        "rejected_to_executing": "REJECTED is followed by EXECUTING in the same sequence group.",
        "scanned_no_setup_to_executing": "SCANNED_NO_SETUP is followed by EXECUTING in the same sequence group.",
        "terminal_followed_by_executing": "Terminal event is followed by an active execution event.",
        "watch_to_sl_without_active_context": "WATCH reaches SL_HIT/STOPPED without TRIGGERED, CONFIRMED, or EXECUTING context.",
        "watch_to_tp_without_active_context": "WATCH reaches TP_HIT without TRIGGERED, CONFIRMED, or EXECUTING context.",
    }
    return messages.get(code, f"Suspicious replay sequence transition {previous} -> {current}.")


def _timestamp_key(value: str) -> tuple[int, Any]:
    if value == NA:
        return (0, "")
    parsed_text = value.replace("Z", "+00:00")
    try:
        return (1, datetime.fromisoformat(parsed_text))
    except ValueError:
        return (2, value)


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if isinstance(row, Mapping):
        return {str(key): _jsonable(value) for key, value in row.items()}
    if is_dataclass(row):
        return {str(key): _jsonable(value) for key, value in asdict(row).items()}
    model_dump = getattr(row, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return {str(key): _jsonable(value) for key, value in dumped.items()}
    return None


def _first_text(data: Mapping[str, Any], field_names: Sequence[str]) -> str:
    for field_name in field_names:
        text = _text(data.get(field_name))
        if text != NA:
            return text
    return NA


def _first_present_text(values: Sequence[Any] | Any) -> str:
    for value in values:
        text = _text(value)
        if text != NA:
            return text
    return NA


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text != NA:
            return text
    return NA


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, Mapping))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    return text in {"1", "true", "yes", "y"}


def _present(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_present(item) for item in value.values())
    if _is_sequence(value):
        return any(_present(item) for item in value)
    return _text(value) != NA


def _text(value: Any) -> str:
    if value is None:
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    text = str(value).strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return NA
    return text


def _uppercase(value: str) -> str:
    return value.upper() if value != NA else NA


def _lowercase(value: str) -> str:
    return value.lower() if value != NA else NA


def _status_key(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return ""
    key = text.strip().replace("-", "_").replace(" ", "_").lower()
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _unique_strings(values: Sequence[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        text = _text(value)
        if text != NA and text not in output:
            output.append(text)
    return output


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return str(value)
    return value


__all__ = [
    "REPLAY_EVENT_SEQUENCE_SCHEMA_VERSION",
    "ReplaySequenceGroup",
    "ReplaySequenceIssue",
    "ReplaySequenceTransition",
    "ReplaySequenceValidationResult",
    "ReplaySequenceValidationSummary",
    "classify_sequence_event_type",
    "normalize_sequence_status",
    "replay_sequence_validation_result_to_dict",
    "validate_replay_event_sequence",
    "validate_replay_event_sequence_from_files",
]
