from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

LIFECYCLE_REPLAY_AUDIT_SAFETY_NOTE = (
    "Lifecycle replay readiness audit is read-only. It does not mutate watch state, scan history, "
    "lifecycle state, performance memory, scanner results, trade ideas, alerts, or database records; "
    "it does not call exchanges, send Telegram messages, or execute trades."
)

IssueSeverity = Literal["info", "warning", "error"]

UPPER_KNOWN_STATUSES = {
    "CANCELLED",
    "CLOSED",
    "CONFIRMED",
    "COOLDOWN",
    "DISCOVERED",
    "EXECUTING",
    "EXPIRED",
    "INVALIDATED",
    "MANAGING",
    "REJECTED",
    "SL_HIT",
    "STALKING",
    "STOPPED",
    "TP_HIT",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "TRIGGERED",
    "WATCH",
}
LOWER_KNOWN_STATUSES = {
    "alert_created",
    "journal_entry_created",
    "scan_error",
    "scanned_no_setup",
    "trade_idea_created",
}
KNOWN_STATUSES = UPPER_KNOWN_STATUSES | LOWER_KNOWN_STATUSES
LIFECYCLE_STATUSES = UPPER_KNOWN_STATUSES - {"DISCOVERED"}
ACTIVE_TRADE_STATUSES = {"TRIGGERED", "CONFIRMED", "EXECUTING", "MANAGING"}
TERMINAL_OUTCOME_STATUSES = {"TP_HIT", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "STOPPED", "CLOSED"}
PASSIVE_OR_REJECTION_STATUSES = {
    "CANCELLED",
    "DISCOVERED",
    "REJECTED",
    "WATCH",
    "scan_error",
    "scanned_no_setup",
}

STATUS_ALIASES = {
    "alert_dry_run_created": "alert_created",
    "alert_sent": "alert_created",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "closed": "CLOSED",
    "confirmed": "CONFIRMED",
    "cooldown": "COOLDOWN",
    "discovered": "DISCOVERED",
    "executing": "EXECUTING",
    "expired": "EXPIRED",
    "hot_watch": "WATCH",
    "idea_created": "trade_idea_created",
    "invalidated": "INVALIDATED",
    "journal_created": "journal_entry_created",
    "journal_entry_created": "journal_entry_created",
    "managing": "MANAGING",
    "no_setup": "scanned_no_setup",
    "rejected": "REJECTED",
    "scan_error": "scan_error",
    "scanned_no_setup": "scanned_no_setup",
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
    "trade_idea_created": "trade_idea_created",
    "triggered": "TRIGGERED",
    "valid_setup": "CONFIRMED",
    "watch": "WATCH",
    "watching": "WATCH",
    "watchlist": "WATCH",
    "watchlisted": "WATCH",
    "watchlist_near_miss": "WATCH",
}

ALLOWED_TRANSITIONS = {
    "DISCOVERED": {"WATCH", "REJECTED"},
    "REJECTED": {"WATCH", "CANCELLED", "CLOSED"},
    "WATCH": {"STALKING", "REJECTED", "CANCELLED", "INVALIDATED"},
    "STALKING": {"WATCH", "TRIGGERED", "REJECTED", "CANCELLED", "INVALIDATED"},
    "TRIGGERED": {"STALKING", "CONFIRMED", "INVALIDATED", "CANCELLED", "STOPPED"},
    "CONFIRMED": {"EXECUTING", "INVALIDATED", "EXPIRED", "CANCELLED", "CLOSED"},
    "EXECUTING": {
        "MANAGING",
        "TP_HIT",
        "TP1_HIT",
        "TP2_HIT",
        "TP3_HIT",
        "SL_HIT",
        "STOPPED",
        "INVALIDATED",
        "EXPIRED",
        "CLOSED",
    },
    "MANAGING": {"TP_HIT", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "STOPPED", "INVALIDATED", "EXPIRED", "CLOSED"},
    "TP_HIT": {"COOLDOWN", "CLOSED"},
    "TP1_HIT": {"TP2_HIT", "TP3_HIT", "COOLDOWN", "CLOSED"},
    "TP2_HIT": {"TP3_HIT", "COOLDOWN", "CLOSED"},
    "TP3_HIT": {"COOLDOWN", "CLOSED"},
    "SL_HIT": {"COOLDOWN", "CLOSED"},
    "STOPPED": {"COOLDOWN", "CLOSED"},
    "INVALIDATED": {"COOLDOWN", "CLOSED", "WATCH"},
    "EXPIRED": {"COOLDOWN", "CLOSED"},
    "COOLDOWN": {"WATCH", "CLOSED", "CANCELLED"},
    "CANCELLED": {"CLOSED", "COOLDOWN"},
    "CLOSED": {"COOLDOWN"},
}
SUSPICIOUS_DIRECT_TRANSITIONS = {
    ("COOLDOWN", "EXECUTING"),
    ("INVALIDATED", "EXECUTING"),
    ("REJECTED", "EXECUTING"),
}

STATUS_FIELDS = (
    "current_state",
    "lifecycle_status",
    "state",
    "to_state",
    "status",
    "last_status",
    "readiness_label",
    "display_status",
)
PREVIOUS_STATUS_FIELDS = ("previous_state", "from_state", "previous_status", "previous_lifecycle_status")
TIMESTAMP_FIELDS = (
    "timestamp",
    "created_at",
    "updated_at",
    "scan_timestamp",
    "scanned_at",
    "seen_at",
    "first_seen_at",
    "last_seen_at",
    "last_transition_at",
    "completed_at",
    "started_at",
    "run_timestamp",
)
STABLE_ID_FIELDS = (
    "setup_id",
    "trade_idea_id",
    "alert_id",
    "run_id",
    "scan_run_id",
    "lifecycle_id",
    "setup_fingerprint",
    "id",
)
SYMBOL_FIELDS = ("symbol", "ticker", "market")
MODE_FIELDS = ("mode", "strategy_mode", "strategy", "strategy_name", "setup_mode")
DIRECTION_FIELDS = ("direction", "side", "bias")
ENTRY_FIELDS = ("entry", "entry_zone", "entry_price", "entry_low", "entry_high", "entry_trigger")
STOP_FIELDS = ("stop", "stop_loss", "stop_reference", "stop_price", "invalidation", "invalidation_reason", "cancel_condition")
TARGET_FIELDS = (
    "target",
    "target_price",
    "targets",
    "take_profit",
    "take_profits",
    "take_profit_targets",
    "tp",
    "tp1",
    "tp2",
    "tp3",
    "tp_hit",
    "highest_tp_hit",
)
OUTCOME_FIELDS = ("outcome", "result", "result_r", "final_r", "final_r_multiple", "r_multiple", "tp_hit", "sl_hit")
COLLECTION_FIELDS = (
    "active_watches",
    "events",
    "history",
    "lifecycles",
    "lifecycle_events",
    "lifecycle_records",
    "memory",
    "outcomes",
    "performance",
    "records",
    "results",
    "setup_lifecycle_events",
    "setup_lifecycle_records",
    "symbols",
    "watch",
    "watch_state",
)


class LifecycleReplayAuditIssue(BaseModel):
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"

    model_config = ConfigDict(frozen=True)


class LifecycleReplayRecord(BaseModel):
    source: str = "in_memory"
    path: str = "root"
    symbol: str | None = None
    stable_id: str | None = None
    stable_id_field: str | None = None
    status: str | None = None
    status_field: str | None = None
    previous_status: str | None = None
    previous_status_field: str | None = None
    status_history: tuple[str, ...] | None = None
    status_history_present: bool = False
    status_history_type: str | None = None
    timestamp: str | None = None
    timestamp_field: str | None = None
    mode: str | None = None
    direction: str | None = None
    has_entry: bool = False
    has_stop_or_invalidation: bool = False
    has_target: bool = False
    has_outcome: bool = False
    has_result_r: bool = False
    lifecycle_like: bool = False
    trade_like: bool = False
    strategy_like: bool = False
    observed_fields: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class LifecycleReplayAuditSummary(BaseModel):
    record_count: int = 0
    symbol_count: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    info_count: int = 0
    warning_count: int = 0
    error_count: int = 0

    model_config = ConfigDict(frozen=True)


class LifecycleReplayAuditResult(BaseModel):
    source: str
    is_valid: bool
    record_count: int = 0
    symbol_count: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    info_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    summary: LifecycleReplayAuditSummary
    issues: tuple[LifecycleReplayAuditIssue, ...] = ()
    inspected_fields: tuple[str, ...] = ()
    safety_note: str = LIFECYCLE_REPLAY_AUDIT_SAFETY_NOTE

    model_config = ConfigDict(frozen=True)


def normalize_lifecycle_status(value: Any) -> str:
    text = _optional_text(value)
    if text is None:
        return "N/A"
    key = _status_key(text)
    alias = STATUS_ALIASES.get(key)
    if alias is not None:
        return alias
    if key in LOWER_KNOWN_STATUSES:
        return key
    upper = key.upper()
    if upper in UPPER_KNOWN_STATUSES:
        return upper
    return upper


def extract_lifecycle_records(data: Any, source: str = "in_memory") -> list[LifecycleReplayRecord]:
    records: list[LifecycleReplayRecord] = []
    _extract_records(data, source=source, path="root", records=records, inherited={})
    return records


def audit_lifecycle_records(
    records: list[LifecycleReplayRecord],
    source: str = "in_memory",
) -> LifecycleReplayAuditResult:
    issues: list[LifecycleReplayAuditIssue] = []
    inspected_fields: list[str] = []
    status_counts: Counter[str] = Counter()
    symbols: set[str] = set()

    if not records:
        _add_issue(
            issues,
            "info",
            "no_lifecycle_records",
            "No lifecycle-like records were found in this artifact.",
        )

    for record in records:
        for field in record.observed_fields:
            _remember(inspected_fields, field)
        if record.symbol is not None:
            symbols.add(record.symbol)

        status = normalize_lifecycle_status(record.status)
        if status != "N/A":
            status_counts[status] += 1
        _audit_record_status(record, status, issues)
        _audit_replay_readiness(record, status, issues)

    _add_issue(issues, "info", "lifecycle_record_count", f"Lifecycle replay record count: {len(records)}.")
    return _result(
        source=source,
        record_count=len(records),
        symbol_count=len(symbols),
        status_counts=dict(sorted(status_counts.items())),
        issues=issues,
        inspected_fields=inspected_fields,
    )


def audit_lifecycle_artifact(data: Any, source: str = "in_memory") -> LifecycleReplayAuditResult:
    if not isinstance(data, (Mapping, list)):
        issue = LifecycleReplayAuditIssue(
            severity="warning",
            code="non_container_json",
            message="Top-level JSON is not an object or array, so lifecycle replay structure cannot be inspected.",
            path="root",
        )
        return _result(source=source, record_count=0, symbol_count=0, status_counts={}, issues=[issue], inspected_fields=())
    return audit_lifecycle_records(extract_lifecycle_records(data, source=source), source=source)


def audit_lifecycle_file(path: Path) -> LifecycleReplayAuditResult:
    artifact_path = Path(path)
    source = str(artifact_path)
    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        issue = LifecycleReplayAuditIssue(
            severity="error",
            code="invalid_json",
            message=f"JSON could not be decoded: {exc.msg}",
            path="root",
        )
        return _result(source=source, record_count=0, symbol_count=0, status_counts={}, issues=[issue], inspected_fields=())
    except OSError as exc:
        issue = LifecycleReplayAuditIssue(
            severity="error",
            code="unreadable_json",
            message=f"Artifact could not be read: {exc}",
            path="root",
        )
        return _result(source=source, record_count=0, symbol_count=0, status_counts={}, issues=[issue], inspected_fields=())
    return audit_lifecycle_artifact(data, source=source)


def _extract_records(
    value: Any,
    *,
    source: str,
    path: str,
    records: list[LifecycleReplayRecord],
    inherited: Mapping[str, str | None],
) -> None:
    if isinstance(value, Mapping):
        current_inherited = _inherit_context(value, inherited)
        if _looks_like_record(value, path):
            records.append(_record_from_mapping(value, source=source, path=path, inherited=current_inherited))
            return

        for field in COLLECTION_FIELDS:
            if field not in value:
                continue
            child = value.get(field)
            child_path = field if path == "root" else f"{path}.{field}"
            if isinstance(child, Mapping):
                for key, item in child.items():
                    next_path = f"{child_path}.{key}"
                    child_inherited = {**current_inherited}
                    if field == "symbols":
                        child_inherited["symbol"] = _optional_text(key)
                    _extract_records(item, source=source, path=next_path, records=records, inherited=child_inherited)
            elif isinstance(child, list):
                for index, item in enumerate(child):
                    _extract_records(
                        item,
                        source=source,
                        path=f"{child_path}[{index}]",
                        records=records,
                        inherited=current_inherited,
                    )
        return

    if isinstance(value, list):
        for index, item in enumerate(value):
            _extract_records(item, source=source, path=f"{path}[{index}]", records=records, inherited=inherited)


def _record_from_mapping(
    data: Mapping[str, Any],
    *,
    source: str,
    path: str,
    inherited: Mapping[str, str | None],
) -> LifecycleReplayRecord:
    lifecycle_state = _mapping_or_empty(data.get("lifecycle_state"))
    lifecycle_transition = _mapping_or_empty(data.get("lifecycle_transition"))
    lifecycle_event = _mapping_or_empty(lifecycle_transition.get("event"))
    trade_idea = _mapping_or_empty(data.get("trade_idea"))
    alert_result = _mapping_or_empty(data.get("alert_result"))
    journal_entry = _mapping_or_empty(data.get("journal_entry"))
    replay_result = _mapping_or_empty(data.get("replay_result"))
    sources = (
        ("", data),
        ("lifecycle_state.", lifecycle_state),
        ("lifecycle_transition.", lifecycle_transition),
        ("lifecycle_transition.event.", lifecycle_event),
        ("trade_idea.", trade_idea),
        ("alert_result.", alert_result),
        ("journal_entry.", journal_entry),
        ("replay_result.", replay_result),
    )

    observed_fields: list[str] = []
    symbol, symbol_field = _first_named_text(sources, SYMBOL_FIELDS)
    if symbol is None:
        symbol = inherited.get("symbol")
        symbol_field = "inferred_symbol" if symbol is not None else None
    _remember_optional(observed_fields, symbol_field)

    stable_id, stable_id_field = _first_named_text(sources, STABLE_ID_FIELDS)
    if stable_id is None:
        stable_id = inherited.get("stable_id")
        stable_id_field = inherited.get("stable_id_field")
    _remember_optional(observed_fields, stable_id_field)

    status_sources = (
        ("lifecycle_state.", lifecycle_state),
        ("lifecycle_transition.event.", lifecycle_event),
        ("lifecycle_transition.", lifecycle_transition),
        ("", data),
        ("trade_idea.", trade_idea),
        ("alert_result.", alert_result),
        ("journal_entry.", journal_entry),
        ("replay_result.", replay_result),
    )
    status, status_field = _best_status(status_sources)
    _remember_optional(observed_fields, status_field)
    previous_status, previous_status_field = _first_named_text(status_sources, PREVIOUS_STATUS_FIELDS)
    _remember_optional(observed_fields, previous_status_field)

    status_history, status_history_present, status_history_type = _extract_status_history(data)
    if status_history_present:
        _remember(observed_fields, "status_history")

    timestamp, timestamp_field = _first_named_text(sources, TIMESTAMP_FIELDS)
    if timestamp is None:
        timestamp = inherited.get("timestamp")
        timestamp_field = inherited.get("timestamp_field")
    _remember_optional(observed_fields, timestamp_field)

    mode, mode_field = _first_named_text(sources, MODE_FIELDS)
    _remember_optional(observed_fields, mode_field)
    direction, direction_field = _first_named_text(sources, DIRECTION_FIELDS)
    _remember_optional(observed_fields, direction_field)

    has_entry = _has_any_present(sources, ENTRY_FIELDS)
    has_stop_or_invalidation = _has_any_present(sources, STOP_FIELDS)
    has_target = _has_any_present(sources, TARGET_FIELDS)
    has_outcome = _has_any_present(sources, OUTCOME_FIELDS)
    has_result_r = _has_any_present(sources, ("result_r", "final_r", "final_r_multiple", "r_multiple"))
    for label, present in (
        ("entry", has_entry),
        ("stop_or_invalidation", has_stop_or_invalidation),
        ("target", has_target),
        ("outcome", has_outcome),
        ("result_r", has_result_r),
    ):
        if present:
            _remember(observed_fields, label)

    normalized_status = normalize_lifecycle_status(status)
    lifecycle_like = (
        normalized_status in LIFECYCLE_STATUSES
        or bool(lifecycle_state)
        or bool(lifecycle_transition)
        or any(field in data for field in ("current_state", "previous_state", "lifecycle_status", "to_state", "from_state"))
    )
    trade_like = (
        normalized_status in ACTIVE_TRADE_STATUSES | TERMINAL_OUTCOME_STATUSES | {"trade_idea_created"}
        or has_entry
        or has_stop_or_invalidation
        or has_target
        or bool(trade_idea)
    )
    strategy_like = trade_like or mode is not None or any(field in data for field in MODE_FIELDS)

    return LifecycleReplayRecord(
        source=source,
        path=path,
        symbol=symbol.upper() if symbol is not None else None,
        stable_id=stable_id,
        stable_id_field=stable_id_field,
        status=status,
        status_field=status_field,
        previous_status=previous_status,
        previous_status_field=previous_status_field,
        status_history=status_history,
        status_history_present=status_history_present,
        status_history_type=status_history_type,
        timestamp=timestamp,
        timestamp_field=timestamp_field,
        mode=mode,
        direction=direction.lower() if direction is not None else None,
        has_entry=has_entry,
        has_stop_or_invalidation=has_stop_or_invalidation,
        has_target=has_target,
        has_outcome=has_outcome,
        has_result_r=has_result_r,
        lifecycle_like=lifecycle_like,
        trade_like=trade_like,
        strategy_like=strategy_like,
        observed_fields=tuple(observed_fields),
    )


def _audit_record_status(
    record: LifecycleReplayRecord,
    status: str,
    issues: list[LifecycleReplayAuditIssue],
) -> None:
    if status == "N/A":
        _add_issue(issues, "warning", "missing_status", "Lifecycle replay record is missing a status.", f"{record.path}.status")
    elif status not in KNOWN_STATUSES:
        _add_issue(
            issues,
            "warning",
            "unknown_status",
            f"Status {status} is not a known lifecycle replay status.",
            _field_path(record.path, record.status_field or "status"),
        )

    if record.previous_status is not None:
        previous = normalize_lifecycle_status(record.previous_status)
        if previous not in KNOWN_STATUSES and previous != "N/A":
            _add_issue(
                issues,
                "warning",
                "unknown_previous_status",
                f"Previous status {previous} is not a known lifecycle replay status.",
                _field_path(record.path, record.previous_status_field or "previous_status"),
            )

    if record.status_history_present and record.status_history_type != "list":
        _add_issue(
            issues,
            "error",
            "status_history_not_list",
            "status_history must be a JSON array when present.",
            f"{record.path}.status_history",
        )
        return

    if record.lifecycle_like and not record.status_history_present:
        _add_issue(
            issues,
            "warning",
            "missing_status_history",
            "Lifecycle-like record has no status_history for replay validation.",
            record.path,
        )
    elif record.status_history_present and record.status_history == ():
        _add_issue(
            issues,
            "warning",
            "empty_status_history",
            "status_history is present but empty.",
            f"{record.path}.status_history",
        )

    sequence = list(record.status_history or ())
    if record.previous_status is not None and not sequence:
        sequence.append(normalize_lifecycle_status(record.previous_status))
    if status != "N/A" and (not sequence or sequence[-1] != status):
        if record.status_history_present and sequence:
            _add_issue(
                issues,
                "warning",
                "status_history_current_mismatch",
                "Final status_history item does not match the current status.",
                f"{record.path}.status_history",
            )
        if record.previous_status is not None:
            sequence.append(status)

    for index, item in enumerate(record.status_history or ()):
        if item not in KNOWN_STATUSES and item != "N/A":
            _add_issue(
                issues,
                "warning",
                "unknown_status",
                f"Status history item {item} is not a known lifecycle replay status.",
                f"{record.path}.status_history[{index}]",
            )

    _audit_transition_sequence(record, tuple(sequence), issues)


def _audit_transition_sequence(
    record: LifecycleReplayRecord,
    statuses: tuple[str, ...],
    issues: list[LifecycleReplayAuditIssue],
) -> None:
    lifecycle_statuses = tuple(status for status in statuses if status in LIFECYCLE_STATUSES)
    if len(lifecycle_statuses) < 2:
        return

    warning_count = 0
    for index, (previous, current) in enumerate(zip(lifecycle_statuses, lifecycle_statuses[1:])):
        if previous == current:
            continue
        if (previous, current) in SUSPICIOUS_DIRECT_TRANSITIONS or current not in ALLOWED_TRANSITIONS.get(previous, set()):
            _add_issue(
                issues,
                "warning",
                "suspicious_transition",
                f"Suspicious lifecycle transition {previous} -> {current}.",
                f"{record.path}.status_history[{index + 1}]",
            )
            warning_count += 1

    final_status = lifecycle_statuses[-1]
    if final_status in {"TP_HIT", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"}:
        prior = set(lifecycle_statuses[:-1])
        if "WATCH" in prior and not prior & {"TRIGGERED", "CONFIRMED", "EXECUTING", "MANAGING"}:
            _add_issue(
                issues,
                "warning",
                "terminal_without_trigger",
                f"{final_status} appears after WATCH without TRIGGERED or CONFIRMED in the status history.",
                record.path,
            )
            warning_count += 1

    if warning_count == 0:
        _add_issue(
            issues,
            "info",
            "valid_transition_sequence",
            "Lifecycle transition sequence looks replay-ready for audit purposes.",
            record.path,
        )


def _audit_replay_readiness(
    record: LifecycleReplayRecord,
    status: str,
    issues: list[LifecycleReplayAuditIssue],
) -> None:
    if record.symbol is None:
        _add_issue(issues, "warning", "missing_symbol", "Replay-like record is missing a symbol.", f"{record.path}.symbol")
    if record.stable_id is None:
        _add_issue(
            issues,
            "warning",
            "missing_stable_identifier",
            "Replay-like record is missing a stable setup_id, trade_idea_id, alert_id, lifecycle_id, or run_id.",
            record.path,
        )
    if record.timestamp is None:
        _add_issue(
            issues,
            "warning",
            "missing_timestamp",
            "Replay-like record is missing a timestamp.",
            record.path,
        )

    if status in ACTIVE_TRADE_STATUSES and not record.has_stop_or_invalidation:
        _add_issue(
            issues,
            "warning",
            "missing_invalidation_or_stop",
            f"{status} record is missing stop or invalidation data.",
            record.path,
        )
    if status == "INVALIDATED" and not record.has_stop_or_invalidation:
        _add_issue(
            issues,
            "warning",
            "missing_invalidation",
            "INVALIDATED record is missing invalidation context.",
            record.path,
        )
    if status in TERMINAL_OUTCOME_STATUSES and not (record.has_outcome or record.has_result_r):
        _add_issue(
            issues,
            "warning",
            "missing_terminal_outcome",
            f"{status} record is missing outcome or result_r data.",
            record.path,
        )

    needs_trade_context = record.trade_like and status not in PASSIVE_OR_REJECTION_STATUSES
    if needs_trade_context and record.direction is None:
        _add_issue(
            issues,
            "warning",
            "missing_direction",
            "Trade-like record is missing direction.",
            record.path,
        )
    if record.strategy_like and status not in PASSIVE_OR_REJECTION_STATUSES and record.mode is None:
        _add_issue(
            issues,
            "warning",
            "missing_strategy_mode",
            "Strategy-like record is missing strategy mode.",
            record.path,
        )


def _extract_status_history(data: Mapping[str, Any]) -> tuple[tuple[str, ...] | None, bool, str | None]:
    if "status_history" in data:
        raw_history = data.get("status_history")
        raw_type = type(raw_history).__name__
        if not isinstance(raw_history, list):
            return None, True, raw_type
        return tuple(normalize_lifecycle_status(item) for item in raw_history), True, "list"

    raw_history = data.get("history")
    if isinstance(raw_history, list):
        statuses: list[str] = []
        for item in raw_history:
            if isinstance(item, Mapping):
                status, _field = _best_status((("", item),))
                normalized = normalize_lifecycle_status(status)
                if normalized != "N/A":
                    statuses.append(normalized)
            else:
                normalized = normalize_lifecycle_status(item)
                if normalized != "N/A":
                    statuses.append(normalized)
        if statuses:
            return tuple(statuses), True, "list"
    return None, False, None


def _best_status(sources: tuple[tuple[str, Mapping[str, Any]], ...]) -> tuple[str | None, str | None]:
    fallback: tuple[str | None, str | None] = (None, None)
    for prefix, source in sources:
        for field in STATUS_FIELDS:
            text = _optional_text(source.get(field))
            if text is None:
                continue
            normalized = normalize_lifecycle_status(text)
            candidate = (text, f"{prefix}{field}")
            if normalized in LIFECYCLE_STATUSES or normalized in LOWER_KNOWN_STATUSES:
                return candidate
            if fallback == (None, None):
                fallback = candidate
    return fallback


def _first_named_text(
    sources: tuple[tuple[str, Mapping[str, Any]], ...],
    fields: tuple[str, ...],
) -> tuple[str | None, str | None]:
    for prefix, source in sources:
        for field in fields:
            text = _optional_text(source.get(field))
            if text is not None:
                return text, f"{prefix}{field}"
    return None, None


def _has_any_present(sources: tuple[tuple[str, Mapping[str, Any]], ...], fields: tuple[str, ...]) -> bool:
    for _prefix, source in sources:
        for field in fields:
            if field in source and _present_value(source.get(field)):
                return True
    return False


def _inherit_context(data: Mapping[str, Any], inherited: Mapping[str, str | None]) -> dict[str, str | None]:
    sources = (("", data),)
    stable_id, stable_id_field = _first_named_text(sources, STABLE_ID_FIELDS)
    timestamp, timestamp_field = _first_named_text(sources, TIMESTAMP_FIELDS)
    symbol, _symbol_field = _first_named_text(sources, SYMBOL_FIELDS)
    return {
        "stable_id": stable_id or inherited.get("stable_id"),
        "stable_id_field": stable_id_field or inherited.get("stable_id_field"),
        "timestamp": timestamp or inherited.get("timestamp"),
        "timestamp_field": timestamp_field or inherited.get("timestamp_field"),
        "symbol": symbol or inherited.get("symbol"),
    }


def _looks_like_record(data: Mapping[str, Any], path: str) -> bool:
    keys = set(data)
    if keys & {
        "current_state",
        "display_status",
        "from_state",
        "last_status",
        "lifecycle_state",
        "lifecycle_status",
        "lifecycle_transition",
        "previous_state",
        "readiness_label",
        "status",
        "status_history",
        "to_state",
    }:
        return True
    if path != "root" and keys & (set(SYMBOL_FIELDS) | set(STABLE_ID_FIELDS)) and keys & (
        set(OUTCOME_FIELDS) | set(ENTRY_FIELDS) | set(STOP_FIELDS) | set(TARGET_FIELDS)
    ):
        return True
    return False


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _status_key(value: str) -> str:
    key = value.strip().replace("-", "_").replace(" ", "_").lower()
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return None
    return text


def _present_value(value: Any) -> bool:
    if _optional_text(value) is not None and not isinstance(value, (Mapping, list, tuple)):
        return True
    if isinstance(value, Mapping):
        return any(_present_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_present_value(item) for item in value)
    return False


def _field_path(path: str, field: str) -> str:
    return field if path == "root" else f"{path}.{field}"


def _result(
    *,
    source: str,
    record_count: int,
    symbol_count: int,
    status_counts: Mapping[str, int],
    issues: list[LifecycleReplayAuditIssue],
    inspected_fields: tuple[str, ...] | list[str],
) -> LifecycleReplayAuditResult:
    counts = Counter(issue.severity for issue in issues)
    normalized_status_counts = dict(status_counts)
    summary = LifecycleReplayAuditSummary(
        record_count=record_count,
        symbol_count=symbol_count,
        status_counts=normalized_status_counts,
        info_count=counts["info"],
        warning_count=counts["warning"],
        error_count=counts["error"],
    )
    return LifecycleReplayAuditResult(
        source=source,
        is_valid=counts["error"] == 0,
        record_count=record_count,
        symbol_count=symbol_count,
        status_counts=normalized_status_counts,
        info_count=counts["info"],
        warning_count=counts["warning"],
        error_count=counts["error"],
        summary=summary,
        issues=tuple(issues),
        inspected_fields=tuple(inspected_fields),
    )


def _add_issue(
    issues: list[LifecycleReplayAuditIssue],
    severity: IssueSeverity,
    code: str,
    message: str,
    path: str = "root",
) -> None:
    issues.append(LifecycleReplayAuditIssue(severity=severity, code=code, message=message, path=path))


def _remember(inspected_fields: list[str], field: str) -> None:
    if field not in inspected_fields:
        inspected_fields.append(field)


def _remember_optional(inspected_fields: list[str], field: str | None) -> None:
    if field is not None:
        _remember(inspected_fields, field)


__all__ = [
    "LIFECYCLE_REPLAY_AUDIT_SAFETY_NOTE",
    "LifecycleReplayAuditIssue",
    "LifecycleReplayAuditResult",
    "LifecycleReplayAuditSummary",
    "LifecycleReplayRecord",
    "audit_lifecycle_artifact",
    "audit_lifecycle_file",
    "audit_lifecycle_records",
    "extract_lifecycle_records",
    "normalize_lifecycle_status",
]
