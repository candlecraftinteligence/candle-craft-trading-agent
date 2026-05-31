from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

NA = "N/A"
OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION = "outcome_event_capture_v1"

OUTCOME_EVENT_CAPTURE_SAFETY_NOTE = (
    "Outcome event capture is local research infrastructure only. It does not calculate PnL, "
    "win rate, expectancy, edge, or profitability; it does not create signals, place or simulate "
    "orders, call exchanges, send Telegram messages, mutate scanner or lifecycle artifacts, "
    "change setup gates, infer outcomes, infer exit prices, invent candles, or invent market data."
)

IssueSeverity = Literal["warning", "error", "blocker"]

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

STABLE_EVENT_ID_FIELDS = (
    "source",
    "capture_source",
    "candidate_id",
    "symbol",
    "timeframe",
    "setup_id",
    "trade_idea_id",
    "alert_id",
    "run_id",
    "scan_id",
    "outcome_status",
    "terminal_reason",
    "outcome_timestamp",
    "closed_at",
)


@dataclass(frozen=True)
class OutcomeEventIssue:
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"
    event_id: str = NA
    field_name: str = NA
    line_number: int = 0


@dataclass(frozen=True)
class OutcomeEventRecord:
    schema_version: str = OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION
    event_id: str = NA
    source: str = NA
    capture_source: str = NA
    captured_at: str = NA
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
    notes: str = NA
    tags: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutcomeEventCaptureSummary:
    total_events: int = 0
    valid_events: int = 0
    invalid_events: int = 0
    terminal_events: int = 0
    open_events: int = 0
    negative_example_events: int = 0
    events_with_result_r: int = 0
    outcome_status_counts: dict[str, int] = field(default_factory=dict)
    terminal_reason_counts: dict[str, int] = field(default_factory=dict)
    symbol_count: int = 0
    strategy_mode_counts: dict[str, int] = field(default_factory=dict)
    warning_count: int = 0
    blocker_count: int = 0
    error_count: int = 0
    is_valid: bool = True
    safety_note: str = OUTCOME_EVENT_CAPTURE_SAFETY_NOTE


@dataclass(frozen=True)
class OutcomeEventAppendResult:
    path: str
    appended: bool = False
    record: OutcomeEventRecord | None = None
    issues: tuple[OutcomeEventIssue, ...] = ()
    summary: OutcomeEventCaptureSummary = field(default_factory=OutcomeEventCaptureSummary)
    schema_version: str = OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION


@dataclass(frozen=True)
class OutcomeEventReadResult:
    path: str
    records: tuple[OutcomeEventRecord, ...] = ()
    issues: tuple[OutcomeEventIssue, ...] = ()
    summary: OutcomeEventCaptureSummary = field(default_factory=OutcomeEventCaptureSummary)
    schema_version: str = OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION


def build_outcome_event_record(payload: dict[str, Any], source: str = "manual") -> OutcomeEventRecord:
    data = _row_to_dict(payload) or {}
    data["schema_version"] = OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION
    record = _record_from_mapping(data, source=source)
    normalized, _issues = _validate_record(record, index=0, source=record.source)
    return normalized


def validate_outcome_event_record(
    record: OutcomeEventRecord | dict[str, Any],
    source: str = "in_memory",
) -> list[OutcomeEventIssue]:
    normalized = _coerce_record(record, source=source)
    _record, issues = _validate_record(normalized, index=0, source=source)
    return list(issues)


def append_outcome_event(path: Path, record: OutcomeEventRecord | dict[str, Any]) -> OutcomeEventAppendResult:
    normalized = _coerce_record(record, source=str(path))
    normalized, issues = _validate_record(normalized, index=0, source=str(path))
    summary = summarize_outcome_events([normalized], source=str(path))
    output_path = Path(path)

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(outcome_event_record_to_dict(normalized), sort_keys=True, separators=(",", ":"))
        with output_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        error = OutcomeEventIssue(
            severity="error",
            code="append_failed",
            message=f"Outcome event append failed: {exc}",
            path=str(output_path),
            event_id=normalized.event_id,
        )
        return OutcomeEventAppendResult(
            path=str(output_path),
            appended=False,
            record=normalized,
            issues=tuple(issues) + (error,),
            summary=_summary_with_extra_issues(summary, (error,)),
        )

    return OutcomeEventAppendResult(
        path=str(output_path),
        appended=True,
        record=normalized,
        issues=tuple(issues),
        summary=summary,
    )


def read_outcome_events(path: Path) -> OutcomeEventReadResult:
    input_path = Path(path)
    read_issues: list[OutcomeEventIssue] = []
    record_issues: list[OutcomeEventIssue] = []
    records: list[OutcomeEventRecord] = []

    if not input_path.exists():
        warning = OutcomeEventIssue(
            severity="warning",
            code="file_missing",
            message=f"Outcome event file not found: {input_path}",
            path=str(input_path),
        )
        summary = _summary_with_extra_issues(summarize_outcome_events([], source=str(input_path)), (warning,))
        return OutcomeEventReadResult(path=str(input_path), records=(), issues=(warning,), summary=summary)

    try:
        lines = input_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        error = OutcomeEventIssue(
            severity="error",
            code="read_failed",
            message=f"Outcome event read failed: {exc}",
            path=str(input_path),
        )
        summary = _summary_with_extra_issues(summarize_outcome_events([], source=str(input_path)), (error,))
        return OutcomeEventReadResult(path=str(input_path), records=(), issues=(error,), summary=summary)

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            read_issues.append(
                OutcomeEventIssue(
                    severity="error",
                    code="invalid_jsonl",
                    message=f"Invalid JSONL on line {line_number}: {exc.msg}",
                    path=str(input_path),
                    line_number=line_number,
                )
            )
            continue
        if not isinstance(payload, Mapping):
            read_issues.append(
                OutcomeEventIssue(
                    severity="error",
                    code="invalid_jsonl_record",
                    message=f"Outcome event line {line_number} must be a JSON object.",
                    path=str(input_path),
                    line_number=line_number,
                )
            )
            continue
        record = _coerce_record(dict(payload), source=str(input_path))
        normalized, validation_issues = _validate_record(record, index=len(records), source=str(input_path))
        records.append(normalized)
        record_issues.extend(
            replace(issue, line_number=line_number)
            for issue in validation_issues
        )

    issues = tuple(read_issues) + tuple(record_issues)
    summary = _summary_with_extra_issues(summarize_outcome_events(list(records), source=str(input_path)), tuple(read_issues))
    return OutcomeEventReadResult(
        path=str(input_path),
        records=tuple(records),
        issues=issues,
        summary=summary,
    )


def summarize_outcome_events(records: list[Any], source: str = "in_memory") -> OutcomeEventCaptureSummary:
    if not _is_sequence(records):
        return OutcomeEventCaptureSummary(total_events=0, error_count=1, is_valid=False)

    normalized_records: list[OutcomeEventRecord] = []
    issues: list[OutcomeEventIssue] = []
    for index, item in enumerate(records):
        data = _row_to_dict(item)
        if data is None:
            issues.append(
                OutcomeEventIssue(
                    severity="error",
                    code="invalid_record_input",
                    message="Outcome event summary input must contain mappings or outcome event records.",
                    path=f"records[{index}]",
                )
            )
            continue
        record = _coerce_record(data, source=source)
        normalized, record_issues = _validate_record(record, index=index, source=source)
        normalized_records.append(normalized)
        issues.extend(record_issues)

    warning_count = sum(len(record.warnings) for record in normalized_records)
    blocker_count = sum(len(record.blockers) for record in normalized_records)
    error_count = sum(1 for issue in issues if issue.severity == "error")
    invalid_events = sum(1 for record in normalized_records if record.blockers)
    symbols = {record.symbol for record in normalized_records if _present(record.symbol)}

    return OutcomeEventCaptureSummary(
        total_events=len(normalized_records),
        valid_events=max(0, len(normalized_records) - invalid_events),
        invalid_events=invalid_events,
        terminal_events=sum(1 for record in normalized_records if record.outcome_status in TERMINAL_OUTCOME_STATUSES),
        open_events=sum(1 for record in normalized_records if record.outcome_status == "OPEN"),
        negative_example_events=sum(1 for record in normalized_records if record.outcome_status in NEGATIVE_OUTCOME_STATUSES),
        events_with_result_r=sum(1 for record in normalized_records if _present(record.result_r)),
        outcome_status_counts=dict(sorted(Counter(record.outcome_status for record in normalized_records).items())),
        terminal_reason_counts=dict(sorted(Counter(record.terminal_reason for record in normalized_records).items())),
        symbol_count=len(symbols),
        strategy_mode_counts=dict(sorted(Counter(record.strategy_mode for record in normalized_records).items())),
        warning_count=warning_count,
        blocker_count=blocker_count,
        error_count=error_count,
        is_valid=error_count == 0 and blocker_count == 0,
    )


def outcome_event_record_to_dict(record: OutcomeEventRecord) -> dict:
    return _jsonable(asdict(record))


def outcome_event_summary_to_dict(summary: OutcomeEventCaptureSummary) -> dict:
    return _jsonable(asdict(summary))


def _record_from_mapping(data: Mapping[str, Any], *, source: str) -> OutcomeEventRecord:
    row_source = _first_non_na(_first_text(data, ("source",)), source)
    capture_source = _first_non_na(_first_text(data, ("capture_source",)), row_source)
    outcome_status = _normalize_outcome_status(_first_text(data, ("outcome_status", "outcome", "status")))
    terminal_reason = _normalize_terminal_reason(
        _first_text(data, ("terminal_reason", "outcome_reason", "close_reason", "exit_reason"))
    )

    record_without_id = OutcomeEventRecord(
        schema_version=_first_non_na(_first_text(data, ("schema_version",)), OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION),
        event_id=NA,
        source=row_source,
        capture_source=capture_source,
        captured_at=_first_text(data, ("captured_at",)),
        candidate_id=_first_text(data, ("candidate_id",)),
        symbol=_uppercase(_first_text(data, ("symbol", "ticker", "market"))),
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
        outcome_status=outcome_status,
        terminal_reason=terminal_reason,
        outcome_timestamp=_first_text(data, ("outcome_timestamp", "terminal_timestamp")),
        closed_at=_first_text(data, ("closed_at",)),
        exit_price=_first_text(data, ("exit_price",)),
        resolved_price=_first_text(data, ("resolved_price",)),
        result_r=_first_text(data, ("result_r", "final_r", "final_r_multiple", "r_multiple")),
        max_favorable_r=_first_text(data, ("max_favorable_r", "mfe_r")),
        max_adverse_r=_first_text(data, ("max_adverse_r", "mae_r")),
        time_to_outcome_minutes=_non_negative_int(data.get("time_to_outcome_minutes")),
        terminal_lifecycle_status=_first_text(data, ("terminal_lifecycle_status",)),
        notes=_first_text(data, ("notes", "capture_notes")),
        tags=tuple(_sequence_text(data.get("tags"))),
        warnings=tuple(_sequence_text(data.get("warnings"))),
        blockers=tuple(_sequence_text(data.get("blockers"))),
    )
    explicit_event_id = _first_text(data, ("event_id",))
    event_id = explicit_event_id if explicit_event_id != NA else _event_id(record_without_id, data)
    return replace(record_without_id, event_id=event_id)


def _validate_record(
    record: OutcomeEventRecord,
    *,
    index: int,
    source: str,
) -> tuple[OutcomeEventRecord, tuple[OutcomeEventIssue, ...]]:
    warnings = list(record.warnings)
    blockers = list(record.blockers)
    issues: list[OutcomeEventIssue] = []
    path = f"records[{index}]"

    def add_warning(code: str, message: str, field_name: str) -> None:
        warnings.append(message)
        issues.append(
            OutcomeEventIssue(
                severity="warning",
                code=code,
                message=message,
                path=path,
                event_id=record.event_id,
                field_name=field_name,
            )
        )

    def add_blocker(code: str, message: str, field_name: str) -> None:
        blockers.append(message)
        issues.append(
            OutcomeEventIssue(
                severity="blocker",
                code=code,
                message=message,
                path=path,
                event_id=record.event_id,
                field_name=field_name,
            )
        )

    def block_and_warn(code: str, message: str, field_name: str) -> None:
        add_blocker(code, message, field_name)
        add_warning(code, message, field_name)

    if record.schema_version != OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION:
        block_and_warn(
            "invalid_schema_version",
            f"schema_version must be {OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION}.",
            "schema_version",
        )

    if record.outcome_status not in ALLOWED_OUTCOME_STATUSES:
        block_and_warn(
            "invalid_outcome_status",
            f"outcome_status {record.outcome_status} is not allowed by the outcome event capture contract.",
            "outcome_status",
        )

    if record.terminal_reason not in ALLOWED_TERMINAL_REASONS:
        block_and_warn(
            "invalid_terminal_reason",
            f"terminal_reason {record.terminal_reason} is not allowed by the outcome event capture contract.",
            "terminal_reason",
        )

    if _present(record.result_r) and not _numeric_like(record.result_r):
        block_and_warn(
            "invalid_result_r",
            "result_r must be numeric-like or N/A when explicitly supplied.",
            "result_r",
        )

    if record.outcome_status in TERMINAL_OUTCOME_STATUSES:
        if not (_present(record.outcome_timestamp) or _present(record.closed_at)):
            block_and_warn(
                "terminal_timestamp_missing",
                "terminal outcome requires outcome_timestamp or closed_at.",
                "outcome_timestamp",
            )
        if not _present(record.terminal_reason):
            block_and_warn(
                "terminal_reason_missing",
                "terminal outcome requires terminal_reason.",
                "terminal_reason",
            )
        if record.outcome_status in PRICE_REQUIRED_TERMINAL_STATUSES and not (
            _present(record.exit_price) or _present(record.resolved_price)
        ):
            block_and_warn(
                "exit_price_missing",
                "terminal outcome requires exit_price or resolved_price for this status.",
                "exit_price",
            )

    normalized = replace(
        record,
        warnings=tuple(_unique_strings(warnings)),
        blockers=tuple(_unique_strings(blockers)),
    )
    return normalized, tuple(issues)


def _coerce_record(record: OutcomeEventRecord | Mapping[str, Any], *, source: str) -> OutcomeEventRecord:
    if isinstance(record, OutcomeEventRecord):
        return record
    data = _row_to_dict(record) or {}
    return _record_from_mapping(data, source=source)


def _event_id(record: OutcomeEventRecord, payload: Mapping[str, Any]) -> str:
    stable_parts = [getattr(record, field_name) for field_name in STABLE_EVENT_ID_FIELDS]
    payload_without_event_id = {str(key): _jsonable(value) for key, value in payload.items() if str(key) != "event_id"}
    payload_json = json.dumps(payload_without_event_id, sort_keys=True, separators=(",", ":"), default=str)
    basis = "\x1f".join([_text(part) for part in stable_parts] + [payload_json])
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]
    return f"oe-{digest}"


def _summary_with_extra_issues(
    summary: OutcomeEventCaptureSummary,
    issues: Sequence[OutcomeEventIssue],
) -> OutcomeEventCaptureSummary:
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    blockers = sum(1 for issue in issues if issue.severity == "blocker")
    errors = sum(1 for issue in issues if issue.severity == "error")
    return replace(
        summary,
        warning_count=summary.warning_count + warnings,
        blocker_count=summary.blocker_count + blockers,
        error_count=summary.error_count + errors,
        is_valid=(summary.error_count + errors) == 0 and (summary.blocker_count + blockers) == 0,
    )


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


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if isinstance(row, Mapping):
        return {str(key): _jsonable(value) for key, value in row.items()}
    if isinstance(row, OutcomeEventRecord):
        return {field_info.name: _jsonable(getattr(row, field_info.name)) for field_info in fields(OutcomeEventRecord)}
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


def _numeric_like(value: Any) -> bool:
    try:
        Decimal(_text(value))
    except Exception:
        return False
    return True


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
    if isinstance(value, Path):
        return str(value)
    return value


__all__ = [
    "OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION",
    "OutcomeEventAppendResult",
    "OutcomeEventCaptureSummary",
    "OutcomeEventIssue",
    "OutcomeEventReadResult",
    "OutcomeEventRecord",
    "append_outcome_event",
    "build_outcome_event_record",
    "outcome_event_record_to_dict",
    "outcome_event_summary_to_dict",
    "read_outcome_events",
    "summarize_outcome_events",
    "validate_outcome_event_record",
]
