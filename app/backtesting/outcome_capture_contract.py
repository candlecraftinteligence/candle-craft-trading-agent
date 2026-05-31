from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from decimal import Decimal
from typing import Any, Literal

NA = "N/A"
OUTCOME_CAPTURE_SCHEMA_VERSION = "outcome_capture_v1"

OUTCOME_CAPTURE_SAFETY_NOTE = (
    "Outcome capture is a read-only record and validation contract. It does not execute replay, "
    "simulate trades, create signals, place orders, call exchanges, send Telegram messages, mutate "
    "scanner or lifecycle artifacts, alter setup gates, infer missing prices, calculate PnL, "
    "calculate win rate, claim edge, or calculate expectancy."
)

IssueSeverity = Literal["warning", "error", "blocker"]
FieldCategory = Literal["identity", "setup", "outcome", "metadata"]

ALLOWED_OUTCOME_STATUSES = {
    NA,
    "OPEN",
    "TP_HIT",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "SL_HIT",
    "INVALIDATED",
    "CANCELLED",
    "EXPIRED",
    "CLOSED_MANUAL",
    "CLOSED_UNKNOWN",
    "NO_SETUP",
    "REJECTED",
    "SCAN_ERROR",
    "UNKNOWN",
}
ALLOWED_TERMINAL_REASONS = {
    NA,
    "take_profit",
    "stop_loss",
    "invalidation",
    "manual_close",
    "timeout",
    "setup_expired",
    "no_setup",
    "rejected",
    "scan_error",
    "data_missing",
    "unknown",
}
ALLOWED_CAPTURE_STATUSES = {"draft", "incomplete", "captured", "rejected_negative_example", "invalid"}

NEGATIVE_OUTCOME_STATUSES = {"NO_SETUP", "REJECTED", "SCAN_ERROR"}
TERMINAL_OUTCOME_STATUSES = {
    "TP_HIT",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "SL_HIT",
    "INVALIDATED",
    "CANCELLED",
    "EXPIRED",
    "CLOSED_MANUAL",
    "CLOSED_UNKNOWN",
}
PRICE_REQUIRED_TERMINAL_STATUSES = {
    "TP_HIT",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "SL_HIT",
    "INVALIDATED",
    "CLOSED_MANUAL",
}
UNKNOWN_OUTCOME_STATUSES = {NA, "UNKNOWN", "CLOSED_UNKNOWN"}

STATUS_FIELDS = ("status", "normalized_lifecycle_status", "lifecycle_status", "current_state", "state")
TIMESTAMP_FIELDS = (
    "scan_timestamp",
    "timestamp",
    "event_timestamp",
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
STABLE_IDENTITY_FIELDS = ("run_id", "scan_id", "setup_id", "trade_idea_id", "alert_id", "row_id")

OUTCOME_STATUS_ALIASES = {
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "closed": "CLOSED_UNKNOWN",
    "closed_manual": "CLOSED_MANUAL",
    "closed_unknown": "CLOSED_UNKNOWN",
    "expired": "EXPIRED",
    "invalidated": "INVALIDATED",
    "manual_close": "CLOSED_MANUAL",
    "no_setup": "NO_SETUP",
    "open": "OPEN",
    "rejected": "REJECTED",
    "scan_error": "SCAN_ERROR",
    "scanned_no_setup": "NO_SETUP",
    "sl": "SL_HIT",
    "sl_hit": "SL_HIT",
    "stop_loss_hit": "SL_HIT",
    "stopped": "SL_HIT",
    "take_profit_hit": "TP_HIT",
    "tp": "TP_HIT",
    "tp_hit": "TP_HIT",
    "tp1": "TP1_HIT",
    "tp1_hit": "TP1_HIT",
    "tp2": "TP2_HIT",
    "tp2_hit": "TP2_HIT",
    "tp3": "TP3_HIT",
    "tp3_hit": "TP3_HIT",
    "unknown": "UNKNOWN",
}
TERMINAL_REASON_ALIASES = {
    "cancelled": "unknown",
    "canceled": "unknown",
    "data_missing": "data_missing",
    "expired": "setup_expired",
    "invalidation": "invalidation",
    "invalidated": "invalidation",
    "manual": "manual_close",
    "manual_close": "manual_close",
    "no_setup": "no_setup",
    "rejected": "rejected",
    "scan_error": "scan_error",
    "setup_expired": "setup_expired",
    "sl": "stop_loss",
    "sl_hit": "stop_loss",
    "stop_loss": "stop_loss",
    "stop_loss_hit": "stop_loss",
    "stopped": "stop_loss",
    "take_profit": "take_profit",
    "take_profit_hit": "take_profit",
    "timeout": "timeout",
    "tp": "take_profit",
    "tp_hit": "take_profit",
    "tp1": "take_profit",
    "tp1_hit": "take_profit",
    "tp2": "take_profit",
    "tp2_hit": "take_profit",
    "tp3": "take_profit",
    "tp3_hit": "take_profit",
    "unknown": "unknown",
}


@dataclass(frozen=True)
class OutcomeFieldSpec:
    name: str
    category: FieldCategory
    required: bool = True
    default: Any = NA
    description: str = NA


@dataclass(frozen=True)
class OutcomeCaptureIssue:
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"
    candidate_id: str = NA
    field_name: str = NA


@dataclass(frozen=True)
class OutcomeCaptureRecord:
    schema_version: str = OUTCOME_CAPTURE_SCHEMA_VERSION
    source: str = NA
    candidate_id: str = NA
    symbol: str = NA
    exchange: str = NA
    market_type: str = NA
    timeframe: str = NA
    strategy_name: str = NA
    strategy_mode: str = NA
    direction: str = NA
    setup_id: str = NA
    trade_idea_id: str = NA
    alert_id: str = NA
    run_id: str = NA
    scan_id: str = NA
    entry: str = NA
    entry_low: str = NA
    entry_high: str = NA
    stop: str = NA
    invalidation: str = NA
    tp1: str = NA
    tp2: str = NA
    tp3: str = NA
    best_rr: str = NA
    rr_to_tp2: str = NA
    outcome_status: str = NA
    terminal_reason: str = NA
    outcome_timestamp: str = NA
    closed_at: str = NA
    exit_price: str = NA
    resolved_price: str = NA
    result_r: str = NA
    max_favorable_r: str = NA
    max_adverse_r: str = NA
    time_to_outcome_minutes: int = 0
    terminal_lifecycle_status: str = NA
    capture_status: str = "draft"
    capture_source: str = NA
    capture_notes: str = NA
    missing_required_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutcomeCaptureValidationSummary:
    total_records: int = 0
    valid_records: int = 0
    invalid_records: int = 0
    captured_records: int = 0
    incomplete_records: int = 0
    negative_example_records: int = 0
    terminal_records: int = 0
    open_records: int = 0
    unknown_outcome_records: int = 0
    records_with_result_r: int = 0
    records_missing_result_r: int = 0
    records_missing_exit_price: int = 0
    records_missing_terminal_timestamp: int = 0
    field_missing_counts: dict[str, int] = field(default_factory=dict)
    outcome_status_counts: dict[str, int] = field(default_factory=dict)
    terminal_reason_counts: dict[str, int] = field(default_factory=dict)
    warning_count: int = 0
    error_count: int = 0
    blocker_count: int = 0
    is_valid: bool = True


@dataclass(frozen=True)
class OutcomeCaptureValidationResult:
    source: str
    schema_version: str = OUTCOME_CAPTURE_SCHEMA_VERSION
    summary: OutcomeCaptureValidationSummary = field(default_factory=OutcomeCaptureValidationSummary)
    records: tuple[OutcomeCaptureRecord, ...] = ()
    issues: tuple[OutcomeCaptureIssue, ...] = ()
    safety_note: str = OUTCOME_CAPTURE_SAFETY_NOTE


def default_outcome_field_specs() -> list[OutcomeFieldSpec]:
    return [
        OutcomeFieldSpec("schema_version", "identity", default=OUTCOME_CAPTURE_SCHEMA_VERSION),
        OutcomeFieldSpec("source", "identity"),
        OutcomeFieldSpec("candidate_id", "identity"),
        OutcomeFieldSpec("symbol", "identity"),
        OutcomeFieldSpec("exchange", "identity"),
        OutcomeFieldSpec("market_type", "identity"),
        OutcomeFieldSpec("timeframe", "identity"),
        OutcomeFieldSpec("strategy_name", "identity"),
        OutcomeFieldSpec("strategy_mode", "identity"),
        OutcomeFieldSpec("direction", "identity"),
        OutcomeFieldSpec("setup_id", "identity"),
        OutcomeFieldSpec("trade_idea_id", "identity"),
        OutcomeFieldSpec("alert_id", "identity"),
        OutcomeFieldSpec("run_id", "identity"),
        OutcomeFieldSpec("scan_id", "identity"),
        OutcomeFieldSpec("entry", "setup"),
        OutcomeFieldSpec("entry_low", "setup"),
        OutcomeFieldSpec("entry_high", "setup"),
        OutcomeFieldSpec("stop", "setup"),
        OutcomeFieldSpec("invalidation", "setup"),
        OutcomeFieldSpec("tp1", "setup"),
        OutcomeFieldSpec("tp2", "setup"),
        OutcomeFieldSpec("tp3", "setup"),
        OutcomeFieldSpec("best_rr", "setup"),
        OutcomeFieldSpec("rr_to_tp2", "setup"),
        OutcomeFieldSpec("outcome_status", "outcome"),
        OutcomeFieldSpec("terminal_reason", "outcome"),
        OutcomeFieldSpec("outcome_timestamp", "outcome"),
        OutcomeFieldSpec("closed_at", "outcome"),
        OutcomeFieldSpec("exit_price", "outcome"),
        OutcomeFieldSpec("resolved_price", "outcome"),
        OutcomeFieldSpec("result_r", "outcome"),
        OutcomeFieldSpec("max_favorable_r", "outcome"),
        OutcomeFieldSpec("max_adverse_r", "outcome"),
        OutcomeFieldSpec("time_to_outcome_minutes", "outcome", default=0),
        OutcomeFieldSpec("terminal_lifecycle_status", "outcome"),
        OutcomeFieldSpec("capture_status", "metadata", default="draft"),
        OutcomeFieldSpec("capture_source", "metadata"),
        OutcomeFieldSpec("capture_notes", "metadata"),
        OutcomeFieldSpec("missing_required_fields", "metadata", default=()),
        OutcomeFieldSpec("warnings", "metadata", default=()),
        OutcomeFieldSpec("blockers", "metadata", default=()),
    ]


def build_outcome_capture_record(
    candidate_or_row: Any,
    overrides: dict[str, Any] | None = None,
    source: str = "in_memory",
) -> OutcomeCaptureRecord:
    data = _row_to_dict(candidate_or_row) or {}
    data["schema_version"] = OUTCOME_CAPTURE_SCHEMA_VERSION
    if overrides:
        data.update({str(key): _jsonable(value) for key, value in overrides.items()})
    record = _record_from_mapping(data, source=source)
    result = validate_outcome_capture_record(record, source=record.source)
    return result.records[0] if result.records else record


def validate_outcome_capture_record(
    record: OutcomeCaptureRecord | dict[str, Any],
    source: str = "in_memory",
) -> OutcomeCaptureValidationResult:
    return validate_outcome_capture_records([record], source=source)


def validate_outcome_capture_records(
    records: list[Any],
    source: str = "in_memory",
) -> OutcomeCaptureValidationResult:
    if not _is_sequence(records):
        issue = OutcomeCaptureIssue(
            severity="error",
            code="invalid_records_input",
            message="Outcome capture validation input must be a sequence of records.",
        )
        return _make_result(source=source, records=(), issues=(issue,))

    validated: list[OutcomeCaptureRecord] = []
    issues: list[OutcomeCaptureIssue] = []
    for index, item in enumerate(records):
        data = _row_to_dict(item)
        if data is None:
            issues.append(
                OutcomeCaptureIssue(
                    severity="error",
                    code="invalid_record_input",
                    message="Outcome capture record input must be a mapping, dataclass, or model with model_dump().",
                    path=f"records[{index}]",
                )
            )
            continue
        record = _record_from_mapping(data, source=source)
        normalized, record_issues = _validate_record(record, index=index)
        validated.append(normalized)
        issues.extend(record_issues)

    return _make_result(source=source, records=tuple(validated), issues=tuple(issues))


def outcome_capture_result_to_dict(result: OutcomeCaptureValidationResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def outcome_record_to_dict(record: OutcomeCaptureRecord) -> dict[str, Any]:
    return _jsonable(asdict(record))


def _record_from_mapping(data: Mapping[str, Any], *, source: str) -> OutcomeCaptureRecord:
    row_source = _first_non_na(_text(data.get("source")), _text(source))
    status = _first_text(data, STATUS_FIELDS)
    explicit_outcome_status = _first_text(data, ("outcome_status", "outcome"))
    outcome_status = _normalize_outcome_status(explicit_outcome_status)
    if outcome_status == NA:
        outcome_status = _negative_outcome_from_status(status)

    terminal_lifecycle_status = _first_text(data, ("terminal_lifecycle_status",))
    if terminal_lifecycle_status == NA:
        lifecycle_status = _first_text(data, ("normalized_lifecycle_status", "lifecycle_status", "status"))
        terminal_lifecycle_status = lifecycle_status if _normalize_outcome_status(lifecycle_status) in TERMINAL_OUTCOME_STATUSES else NA

    capture_notes = _first_text(data, ("capture_notes",))
    if capture_notes == NA and outcome_status in NEGATIVE_OUTCOME_STATUSES:
        capture_notes = _first_non_na(_first_rejection_reason(data), _first_text(data, ("first_failed_gate", "failed_gate")))

    capture_source = _first_non_na(_first_text(data, ("capture_source",)), row_source)
    capture_status = _first_text(data, ("capture_status",))
    if capture_status == NA:
        capture_status = _default_capture_status(outcome_status=outcome_status, data=data)

    return OutcomeCaptureRecord(
        schema_version=_first_non_na(_text(data.get("schema_version")), OUTCOME_CAPTURE_SCHEMA_VERSION),
        source=row_source,
        candidate_id=_candidate_id(data, source=row_source),
        symbol=_uppercase(_first_text(data, ("symbol", "ticker"))),
        exchange=_first_text(data, ("exchange", "exchange_name")),
        market_type=_first_text(data, ("market_type",)),
        timeframe=_first_text(data, ("timeframe", "time_frame", "tf")),
        strategy_name=_first_text(data, ("strategy_name", "strategy")),
        strategy_mode=_first_text(data, ("strategy_mode", "setup_mode", "mode")),
        direction=_lowercase(_first_text(data, ("direction", "side", "bias"))),
        setup_id=_first_text(data, ("setup_id", "setup_fingerprint", "lifecycle_id")),
        trade_idea_id=_first_text(data, ("trade_idea_id", "idea_id")),
        alert_id=_first_text(data, ("alert_id",)),
        run_id=_first_text(data, ("run_id", "scan_run_id")),
        scan_id=_first_text(data, ("scan_id",)),
        entry=_first_text(data, ("entry", "entry_price", "entry_trigger")),
        entry_low=_first_text(data, ("entry_low",)),
        entry_high=_first_text(data, ("entry_high",)),
        stop=_first_text(data, ("stop", "stop_loss", "stop_price")),
        invalidation=_first_text(data, ("invalidation", "cancel_condition")),
        tp1=_first_text(data, ("tp1", "target_1", "take_profit_1")),
        tp2=_first_text(data, ("tp2", "target_2", "take_profit_2")),
        tp3=_first_text(data, ("tp3", "target_3", "take_profit_3")),
        best_rr=_first_text(data, ("best_rr", "best_risk_reward_ratio")),
        rr_to_tp2=_first_text(data, ("rr_to_tp2", "target_rr_to_tp2")),
        outcome_status=outcome_status,
        terminal_reason=_normalize_terminal_reason(_first_text(data, ("terminal_reason", "outcome_reason", "close_reason", "exit_reason"))),
        outcome_timestamp=_first_text(data, ("outcome_timestamp", "terminal_timestamp")),
        closed_at=_first_text(data, ("closed_at",)),
        exit_price=_first_text(data, ("exit_price",)),
        resolved_price=_first_text(data, ("resolved_price",)),
        result_r=_first_text(data, ("result_r", "final_r", "final_r_multiple", "r_multiple")),
        max_favorable_r=_first_text(data, ("max_favorable_r", "mfe_r")),
        max_adverse_r=_first_text(data, ("max_adverse_r", "mae_r")),
        time_to_outcome_minutes=_non_negative_int(data.get("time_to_outcome_minutes")),
        terminal_lifecycle_status=terminal_lifecycle_status,
        capture_status=_lowercase(capture_status),
        capture_source=capture_source,
        capture_notes=capture_notes,
        missing_required_fields=tuple(_sequence_text(data.get("missing_required_fields"))),
        warnings=tuple(_sequence_text(data.get("warnings"))),
        blockers=tuple(_sequence_text(data.get("blockers"))),
    )


def _validate_record(record: OutcomeCaptureRecord, *, index: int) -> tuple[OutcomeCaptureRecord, tuple[OutcomeCaptureIssue, ...]]:
    warnings = list(record.warnings)
    blockers = list(record.blockers)
    missing_required = list(record.missing_required_fields)
    issues: list[OutcomeCaptureIssue] = []
    path = f"records[{index}]"

    def add_warning(code: str, message: str, field_name: str) -> None:
        warnings.append(message)
        issues.append(
            OutcomeCaptureIssue(
                severity="warning",
                code=code,
                message=message,
                path=path,
                candidate_id=record.candidate_id,
                field_name=field_name,
            )
        )

    def add_blocker(code: str, message: str, field_name: str) -> None:
        blockers.append(message)
        issues.append(
            OutcomeCaptureIssue(
                severity="blocker",
                code=code,
                message=message,
                path=path,
                candidate_id=record.candidate_id,
                field_name=field_name,
            )
        )

    def mark_missing(field_name: str, message: str) -> None:
        missing_required.append(field_name)
        add_blocker(f"{field_name}_missing", message, field_name)
        add_warning(f"{field_name}_missing", message, field_name)

    if record.schema_version != OUTCOME_CAPTURE_SCHEMA_VERSION:
        message = f"schema_version must be {OUTCOME_CAPTURE_SCHEMA_VERSION}."
        add_blocker("invalid_schema_version", message, "schema_version")
        add_warning("invalid_schema_version", message, "schema_version")

    if record.outcome_status not in ALLOWED_OUTCOME_STATUSES:
        message = f"outcome_status {record.outcome_status} is not allowed by the outcome capture contract."
        add_blocker("invalid_outcome_status", message, "outcome_status")
        add_warning("invalid_outcome_status", message, "outcome_status")

    if record.terminal_reason not in ALLOWED_TERMINAL_REASONS:
        message = f"terminal_reason {record.terminal_reason} is not allowed by the outcome capture contract."
        add_blocker("invalid_terminal_reason", message, "terminal_reason")
        add_warning("invalid_terminal_reason", message, "terminal_reason")

    if record.capture_status not in ALLOWED_CAPTURE_STATUSES:
        message = f"capture_status {record.capture_status} is not allowed by the outcome capture contract."
        add_blocker("invalid_capture_status", message, "capture_status")
        add_warning("invalid_capture_status", message, "capture_status")

    if record.outcome_status == NA:
        add_warning("outcome_status_missing", "outcome_status is N/A; record remains draft/incomplete.", "outcome_status")

    if record.outcome_status in {"UNKNOWN", "CLOSED_UNKNOWN"}:
        add_warning(
            "unknown_outcome_not_validation_ready",
            f"outcome_status {record.outcome_status} is not performance-validation ready.",
            "outcome_status",
        )

    if record.outcome_status in TERMINAL_OUTCOME_STATUSES:
        if not (_present(record.outcome_timestamp) or _present(record.closed_at)):
            mark_missing(
                "terminal_timestamp",
                "terminal outcome requires outcome_timestamp or closed_at.",
            )
        if not _present(record.terminal_reason):
            mark_missing("terminal_reason", "terminal outcome requires terminal_reason.")
        if record.outcome_status in PRICE_REQUIRED_TERMINAL_STATUSES and not (
            _present(record.exit_price) or _present(record.resolved_price)
        ):
            mark_missing(
                "exit_price",
                "terminal outcome requires exit_price or resolved_price for this status.",
            )

    normalized_capture_status = "invalid" if blockers else record.capture_status
    normalized = replace(
        record,
        capture_status=normalized_capture_status,
        missing_required_fields=tuple(_unique_strings(missing_required)),
        warnings=tuple(_unique_strings(warnings)),
        blockers=tuple(_unique_strings(blockers)),
    )
    return normalized, tuple(issues)


def _make_result(
    *,
    source: str,
    records: Sequence[OutcomeCaptureRecord],
    issues: Sequence[OutcomeCaptureIssue],
) -> OutcomeCaptureValidationResult:
    record_tuple = tuple(records)
    warning_count = sum(1 for issue in issues if issue.severity == "warning")
    error_count = sum(1 for issue in issues if issue.severity == "error")
    blocker_count = sum(1 for issue in issues if issue.severity == "blocker")
    invalid_records = sum(1 for record in record_tuple if record.blockers or record.capture_status == "invalid")
    terminal_records = [record for record in record_tuple if record.outcome_status in TERMINAL_OUTCOME_STATUSES]
    price_required_records = [
        record for record in terminal_records if record.outcome_status in PRICE_REQUIRED_TERMINAL_STATUSES
    ]
    field_missing_counts = Counter(
        field_name
        for record in record_tuple
        for field_name in _record_field_names()
        if field_name not in {"missing_required_fields", "warnings", "blockers"}
        and not _present(getattr(record, field_name))
    )
    outcome_status_counts = Counter(record.outcome_status for record in record_tuple)
    terminal_reason_counts = Counter(record.terminal_reason for record in record_tuple)

    summary = OutcomeCaptureValidationSummary(
        total_records=len(record_tuple),
        valid_records=max(0, len(record_tuple) - invalid_records),
        invalid_records=invalid_records,
        captured_records=sum(1 for record in record_tuple if record.capture_status == "captured"),
        incomplete_records=sum(1 for record in record_tuple if record.capture_status == "incomplete"),
        negative_example_records=sum(
            1
            for record in record_tuple
            if record.capture_status == "rejected_negative_example"
            or record.outcome_status in NEGATIVE_OUTCOME_STATUSES
        ),
        terminal_records=len(terminal_records),
        open_records=sum(1 for record in record_tuple if record.outcome_status == "OPEN"),
        unknown_outcome_records=sum(1 for record in record_tuple if record.outcome_status in UNKNOWN_OUTCOME_STATUSES),
        records_with_result_r=sum(1 for record in record_tuple if _present(record.result_r)),
        records_missing_result_r=sum(1 for record in record_tuple if not _present(record.result_r)),
        records_missing_exit_price=sum(
            1 for record in price_required_records if not (_present(record.exit_price) or _present(record.resolved_price))
        ),
        records_missing_terminal_timestamp=sum(
            1 for record in terminal_records if not (_present(record.outcome_timestamp) or _present(record.closed_at))
        ),
        field_missing_counts=dict(sorted(field_missing_counts.items())),
        outcome_status_counts=dict(sorted(outcome_status_counts.items())),
        terminal_reason_counts=dict(sorted(terminal_reason_counts.items())),
        warning_count=warning_count,
        error_count=error_count,
        blocker_count=blocker_count,
        is_valid=error_count == 0 and blocker_count == 0,
    )
    return OutcomeCaptureValidationResult(
        source=_text(source),
        summary=summary,
        records=record_tuple,
        issues=tuple(issues),
    )


def _default_capture_status(*, outcome_status: str, data: Mapping[str, Any]) -> str:
    if outcome_status in NEGATIVE_OUTCOME_STATUSES:
        return "rejected_negative_example"
    if outcome_status in TERMINAL_OUTCOME_STATUSES:
        has_timestamp = _present(_first_text(data, ("outcome_timestamp", "terminal_timestamp", "closed_at")))
        has_reason = _present(_normalize_terminal_reason(_first_text(data, ("terminal_reason", "outcome_reason", "close_reason", "exit_reason"))))
        has_price = _present(_first_text(data, ("exit_price", "resolved_price")))
        price_required = outcome_status in PRICE_REQUIRED_TERMINAL_STATUSES
        return "captured" if has_timestamp and has_reason and (has_price or not price_required) else "incomplete"
    if outcome_status == "OPEN":
        return "draft"
    if _is_trade_like_data(data):
        return "incomplete"
    return "draft"


def _candidate_id(data: Mapping[str, Any], *, source: str) -> str:
    explicit = _first_text(data, ("candidate_id",))
    if explicit != NA:
        return explicit
    stable_values = [_text(data.get(field_name)) for field_name in STABLE_IDENTITY_FIELDS]
    basis = [source]
    if any(_present(value) for value in stable_values):
        basis.extend(stable_values)
    else:
        basis.extend(
            [
                _first_text(data, ("symbol", "ticker")),
                _first_text(data, ("timeframe", "time_frame", "tf")),
                _first_text(data, STATUS_FIELDS),
                _first_text(data, TIMESTAMP_FIELDS),
            ]
        )
    digest = hashlib.sha256("\x1f".join(basis).encode("utf-8")).hexdigest()[:16]
    return f"oc-{digest}"


def _negative_outcome_from_status(status: Any) -> str:
    key = _status_key(status)
    if key in {"no_setup", "scanned_no_setup"}:
        return "NO_SETUP"
    if key == "rejected":
        return "REJECTED"
    if key == "scan_error":
        return "SCAN_ERROR"
    return NA


def _normalize_outcome_status(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return NA
    key = _status_key(text)
    if key in OUTCOME_STATUS_ALIASES:
        return OUTCOME_STATUS_ALIASES[key]
    upper = key.upper()
    return upper if upper in ALLOWED_OUTCOME_STATUSES else text


def _normalize_terminal_reason(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return NA
    key = _status_key(text)
    if key in TERMINAL_REASON_ALIASES:
        return TERMINAL_REASON_ALIASES[key]
    return key if key in ALLOWED_TERMINAL_REASONS else text


def _is_trade_like_data(data: Mapping[str, Any]) -> bool:
    if _truthy(data.get("trade_idea_present")) or _truthy(data.get("alert_present")) or _truthy(
        data.get("journal_entry_present")
    ):
        return True
    if any(_present(data.get(field_name)) for field_name in ("setup_id", "trade_idea_id", "alert_id")):
        return True
    return any(
        _present(data.get(field_name))
        for field_name in ("entry", "entry_low", "entry_high", "stop", "invalidation", "tp1", "tp2", "tp3")
    )


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if isinstance(row, Mapping):
        return {str(key): _jsonable(value) for key, value in row.items()}
    if isinstance(row, OutcomeCaptureRecord):
        return {field_info.name: _jsonable(getattr(row, field_info.name)) for field_info in fields(OutcomeCaptureRecord)}
    if is_dataclass(row):
        return {str(key): _jsonable(value) for key, value in asdict(row).items()}
    model_dump = getattr(row, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return {str(key): _jsonable(value) for key, value in dumped.items()}
    return None


def _record_field_names() -> tuple[str, ...]:
    return tuple(field_info.name for field_info in fields(OutcomeCaptureRecord))


def _first_text(data: Mapping[str, Any], field_names: Sequence[str]) -> str:
    for field_name in field_names:
        text = _text(data.get(field_name))
        if text != NA:
            return text
    return NA


def _first_rejection_reason(data: Mapping[str, Any]) -> str:
    reason = _text(data.get("rejection_reason"))
    if reason != NA:
        return reason
    reasons = _sequence_text(data.get("rejection_reasons"))
    return reasons[0] if reasons else NA


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text != NA:
            return text
    return NA


def _sequence_text(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = _text(value)
        return () if text == NA else (text,)
    if isinstance(value, Mapping):
        return ()
    if _is_sequence(value):
        return tuple(text for item in value if (text := _text(item)) != NA)
    return ()


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
    return not _is_na(value)


def _is_na(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return not text or text.upper() in {"N/A", "NA", "NONE", "NULL"}
    return False


def _text(value: Any) -> str:
    if _is_na(value):
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    return str(value).strip()


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


def _non_negative_int(value: Any) -> int:
    if _is_na(value):
        return 0
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return 0


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
    "OUTCOME_CAPTURE_SCHEMA_VERSION",
    "OutcomeCaptureIssue",
    "OutcomeCaptureRecord",
    "OutcomeCaptureValidationResult",
    "OutcomeCaptureValidationSummary",
    "OutcomeFieldSpec",
    "build_outcome_capture_record",
    "default_outcome_field_specs",
    "outcome_capture_result_to_dict",
    "outcome_record_to_dict",
    "validate_outcome_capture_record",
    "validate_outcome_capture_records",
]
