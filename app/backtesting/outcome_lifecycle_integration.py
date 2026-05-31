from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.backtesting.outcome_event_capture import OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION
from app.backtesting.replay_validation_scaffold import (
    ReplayValidationCandidate,
    build_replay_validation_plan_from_files,
)

NA = "N/A"
OUTCOME_LIFECYCLE_INTEGRATION_SCHEMA_VERSION = "outcome_lifecycle_integration_v1"

OUTCOME_LIFECYCLE_INTEGRATION_SAFETY_NOTE = (
    "Outcome event lifecycle integration is read-only audit infrastructure. It maps local lifecycle "
    "statuses to draft outcome event payloads only; it does not calculate PnL, win rate, expectancy, "
    "edge, or profitability, create signals, place or simulate trades, call exchanges, send Telegram "
    "messages, mutate scanner or lifecycle artifacts, alter setup gates, infer hidden prices, infer "
    "missing candles, infer outcomes, or invent market data."
)

IssueSeverity = Literal["warning", "blocker", "error"]

TERMINAL_OUTCOME_STATUSES = {
    "TP_HIT",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "SL_HIT",
    "INVALIDATED",
    "CANCELLED",
    "EXPIRED",
}
OPEN_OUTCOME_STATUSES = {"OPEN"}
NEGATIVE_OUTCOME_STATUSES = {"NO_SETUP", "REJECTED", "SCAN_ERROR"}
UNKNOWN_OUTCOME_STATUSES = {"UNKNOWN"}

STATUS_FIELDS = (
    "status",
    "lifecycle_status",
    "current_state",
    "state",
    "display_status",
    "readiness_label",
    "last_status",
    "to_state",
    "terminal_lifecycle_status",
)
NORMALIZED_STATUS_FIELDS = ("normalized_lifecycle_status", "normalized_status")
TIMESTAMP_FIELDS = ("outcome_timestamp", "terminal_timestamp", "closed_at")
PRICE_FIELDS = ("exit_price", "resolved_price")
SAFE_SCALAR_PAYLOAD_FIELDS = (
    "candidate_id",
    "symbol",
    "exchange",
    "market_type",
    "timeframe",
    "strategy_name",
    "strategy_mode",
    "direction",
    "setup_id",
    "trade_idea_id",
    "alert_id",
    "run_id",
    "scan_id",
    "outcome_timestamp",
    "closed_at",
    "exit_price",
    "resolved_price",
    "result_r",
)
STABLE_IDENTITY_FIELDS = ("run_id", "scan_id", "setup_id", "trade_idea_id", "alert_id", "row_id")

OUTCOME_STATUS_BY_STATUS_KEY = {
    "tp": "TP_HIT",
    "tp_hit": "TP_HIT",
    "take_profit_hit": "TP_HIT",
    "tp1": "TP1_HIT",
    "tp1_hit": "TP1_HIT",
    "tp_1_hit": "TP1_HIT",
    "tp2": "TP2_HIT",
    "tp2_hit": "TP2_HIT",
    "tp_2_hit": "TP2_HIT",
    "tp3": "TP3_HIT",
    "tp3_hit": "TP3_HIT",
    "tp_3_hit": "TP3_HIT",
    "sl": "SL_HIT",
    "sl_hit": "SL_HIT",
    "stop_loss_hit": "SL_HIT",
    "stopped": "SL_HIT",
    "invalidated": "INVALIDATED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "expired": "EXPIRED",
    "rejected": "REJECTED",
    "scanned_no_setup": "NO_SETUP",
    "no_setup": "NO_SETUP",
    "scan_error": "SCAN_ERROR",
    "watch": "OPEN",
    "watching": "OPEN",
    "watchlist": "OPEN",
    "watchlisted": "OPEN",
    "stalking": "OPEN",
    "triggered": "OPEN",
    "confirmed": "OPEN",
    "executing": "OPEN",
    "trade_idea_created": "OPEN",
    "idea_created": "OPEN",
    "alert_created": "OPEN",
    "alert_dry_run_created": "OPEN",
    "alert_sent": "OPEN",
    "journal_entry_created": "OPEN",
    "journal_created": "OPEN",
    "unknown": "UNKNOWN",
}

TERMINAL_REASON_BY_OUTCOME_STATUS = {
    "TP_HIT": "take_profit",
    "TP1_HIT": "take_profit",
    "TP2_HIT": "take_profit",
    "TP3_HIT": "take_profit",
    "SL_HIT": "stop_loss",
    "INVALIDATED": "invalidation",
    "CANCELLED": "manual_close",
    "EXPIRED": "setup_expired",
    "REJECTED": "rejected",
    "NO_SETUP": "no_setup",
    "SCAN_ERROR": "scan_error",
    "OPEN": NA,
    "UNKNOWN": "unknown",
}


@dataclass(frozen=True)
class OutcomeLifecycleIssue:
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"
    candidate_id: str = NA
    field_name: str = NA


@dataclass(frozen=True)
class OutcomeLifecycleMapping:
    lifecycle_status: str = NA
    normalized_lifecycle_status: str = NA
    mapped_outcome_status: str = "UNKNOWN"
    mapped_terminal_reason: str = "unknown"


@dataclass(frozen=True)
class OutcomeLifecycleCandidate:
    schema_version: str = OUTCOME_LIFECYCLE_INTEGRATION_SCHEMA_VERSION
    source: str = NA
    candidate_id: str = NA
    symbol: str = NA
    timeframe: str = NA
    strategy_mode: str = NA
    direction: str = NA
    lifecycle_status: str = NA
    normalized_lifecycle_status: str = NA
    mapped_outcome_status: str = "UNKNOWN"
    mapped_terminal_reason: str = "unknown"
    is_terminal_lifecycle: bool = False
    is_open_lifecycle: bool = False
    is_negative_example: bool = False
    is_outcome_event_eligible: bool = False
    payload_preview: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutcomeLifecycleIntegrationSummary:
    total_candidates: int = 0
    eligible_candidates: int = 0
    ineligible_candidates: int = 0
    terminal_candidates: int = 0
    open_candidates: int = 0
    negative_example_candidates: int = 0
    unknown_status_candidates: int = 0
    mapped_outcome_status_counts: dict[str, int] = field(default_factory=dict)
    mapped_terminal_reason_counts: dict[str, int] = field(default_factory=dict)
    blocker_counts: dict[str, int] = field(default_factory=dict)
    warning_count: int = 0
    error_count: int = 0
    is_valid: bool = True


@dataclass(frozen=True)
class OutcomeLifecycleIntegrationResult:
    source: str
    schema_version: str = OUTCOME_LIFECYCLE_INTEGRATION_SCHEMA_VERSION
    summary: OutcomeLifecycleIntegrationSummary = field(default_factory=OutcomeLifecycleIntegrationSummary)
    candidates: tuple[OutcomeLifecycleCandidate, ...] = ()
    issues: tuple[OutcomeLifecycleIssue, ...] = ()
    safety_note: str = OUTCOME_LIFECYCLE_INTEGRATION_SAFETY_NOTE


def map_lifecycle_status_to_outcome_status(status: Any, normalized_status: Any = NA) -> str:
    return _mapping_for(status=status, normalized_status=normalized_status).mapped_outcome_status


def map_lifecycle_status_to_terminal_reason(status: Any, normalized_status: Any = NA) -> str:
    return _mapping_for(status=status, normalized_status=normalized_status).mapped_terminal_reason


def build_outcome_event_payload_from_candidate(
    candidate_or_row: Any,
    source: str = "lifecycle_integration",
) -> dict[str, Any]:
    if isinstance(candidate_or_row, OutcomeLifecycleCandidate):
        return _jsonable(candidate_or_row.payload_preview)

    data = _row_to_dict(candidate_or_row) or {}
    row_source = _first_non_na(_first_text(data, ("source",)), source)
    lifecycle_status = _first_text(data, STATUS_FIELDS)
    normalized_status = _first_text(data, NORMALIZED_STATUS_FIELDS)
    mapping = _mapping_for(status=lifecycle_status, normalized_status=normalized_status)

    payload: dict[str, Any] = {
        "schema_version": OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION,
        "source": row_source,
        "capture_source": source,
        "candidate_id": _candidate_id(data, source=row_source, row_index=0),
        "outcome_status": mapping.mapped_outcome_status,
        "terminal_reason": mapping.mapped_terminal_reason,
        "terminal_lifecycle_status": _first_non_na(normalized_status, lifecycle_status),
    }

    for field_name in SAFE_SCALAR_PAYLOAD_FIELDS:
        if field_name in payload:
            continue
        value = _payload_value_for_field(field_name, data)
        payload[field_name] = value

    if payload["symbol"] != NA:
        payload["symbol"] = payload["symbol"].upper()
    if payload["direction"] != NA:
        payload["direction"] = payload["direction"].lower()

    for collection_field in ("warnings", "blockers", "tags"):
        values = _sequence_text(data.get(collection_field))
        if values:
            payload[collection_field] = list(values)

    return _jsonable(payload)


def audit_outcome_lifecycle_candidate(
    candidate_or_row: Any,
    source: str = "in_memory",
) -> OutcomeLifecycleCandidate:
    data = _row_to_dict(candidate_or_row)
    if data is None:
        data = {}
        input_blocker = "candidate input could not be inspected as a mapping or dataclass."
    else:
        input_blocker = NA
    return _candidate_from_mapping(data, row_index=0, source=source, input_blocker=input_blocker)


def audit_outcome_lifecycle_candidates(
    candidates_or_rows: list[Any],
    source: str = "in_memory",
) -> OutcomeLifecycleIntegrationResult:
    if not _is_sequence(candidates_or_rows):
        issue = OutcomeLifecycleIssue(
            severity="error",
            code="invalid_candidates_input",
            message="Outcome lifecycle integration input must be a sequence of candidates or rows.",
        )
        return _make_result(source=source, candidates=(), issues=(issue,))

    candidates: list[OutcomeLifecycleCandidate] = []
    input_issues: list[OutcomeLifecycleIssue] = []
    for row_index, row in enumerate(candidates_or_rows):
        data = _row_to_dict(row)
        input_blocker = NA
        if data is None:
            input_blocker = "candidate input could not be inspected as a mapping or dataclass."
            input_issues.append(
                OutcomeLifecycleIssue(
                    severity="error",
                    code="invalid_candidate_row",
                    message=input_blocker,
                    path=f"candidates[{row_index}]",
                )
            )
            data = {}
        candidates.append(_candidate_from_mapping(data, row_index=row_index, source=source, input_blocker=input_blocker))

    return _make_result(source=source, candidates=tuple(candidates), issues=tuple(input_issues))


def audit_outcome_lifecycle_from_files(paths: list[Path]) -> OutcomeLifecycleIntegrationResult:
    try:
        validation_result = build_replay_validation_plan_from_files(paths)
    except Exception as exc:  # pragma: no cover - defensive for unexpected filesystem errors.
        issue = OutcomeLifecycleIssue(
            severity="error",
            code="input_file_error",
            message=f"Replay validation candidates could not be built: {exc}",
        )
        return _make_result(source="files", candidates=(), issues=(issue,))

    source = validation_result.source
    candidates = tuple(
        _candidate_from_mapping(_row_to_dict(candidate) or {}, row_index=index, source=source)
        for index, candidate in enumerate(validation_result.candidates)
    )
    replay_issues = tuple(_issue_from_replay_validation_issue(issue) for issue in validation_result.issues if _issue_severity(issue) != "info")
    return _make_result(source=source, candidates=candidates, issues=replay_issues)


def outcome_lifecycle_result_to_dict(result: OutcomeLifecycleIntegrationResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def _candidate_from_mapping(
    data: Mapping[str, Any],
    *,
    row_index: int,
    source: str,
    input_blocker: str = NA,
) -> OutcomeLifecycleCandidate:
    row_source = _first_non_na(_first_text(data, ("source",)), source)
    lifecycle_status = _first_text(data, STATUS_FIELDS)
    normalized_status = _first_text(data, NORMALIZED_STATUS_FIELDS)
    mapping = _mapping_for(status=lifecycle_status, normalized_status=normalized_status)
    outcome_status = mapping.mapped_outcome_status
    terminal_reason = mapping.mapped_terminal_reason
    warnings: list[str] = []
    blockers: list[str] = []

    def block_and_warn(message: str) -> None:
        blockers.append(message)
        warnings.append(message)

    symbol = _uppercase(_first_text(data, ("symbol", "ticker", "market")))
    candidate_id = _candidate_id(data, source=row_source, row_index=row_index)

    if input_blocker != NA:
        block_and_warn(input_blocker)
    if not _present(symbol):
        block_and_warn("symbol missing; draft outcome event payload cannot be uniquely attributed.")
    if not (_present(lifecycle_status) or _present(normalized_status)):
        block_and_warn("lifecycle/status information missing; outcome mapping cannot be audited.")
    if outcome_status == "UNKNOWN":
        block_and_warn("lifecycle/status value maps to UNKNOWN; explicit review is required.")

    is_terminal = outcome_status in TERMINAL_OUTCOME_STATUSES
    is_open = outcome_status in OPEN_OUTCOME_STATUSES
    is_negative = outcome_status in NEGATIVE_OUTCOME_STATUSES

    if is_terminal:
        if not _present(terminal_reason):
            block_and_warn("terminal lifecycle mapping is missing terminal_reason.")
        if not _has_any_present(data, TIMESTAMP_FIELDS):
            block_and_warn("terminal lifecycle candidate is missing outcome_timestamp or closed_at.")
        if not _has_any_present(data, PRICE_FIELDS):
            block_and_warn("terminal lifecycle candidate is missing exit_price or resolved_price.")

    severe_blockers = []
    if not _present(symbol):
        severe_blockers.append("symbol")
    if not (_present(lifecycle_status) or _present(normalized_status)):
        severe_blockers.append("status")
    if outcome_status == "UNKNOWN":
        severe_blockers.append("unknown_status")
    if input_blocker != NA:
        severe_blockers.append("invalid_input")

    payload_preview = build_outcome_event_payload_from_candidate(data, source=source)
    payload_preview["candidate_id"] = candidate_id
    if warnings:
        payload_preview["warnings"] = _unique_strings(warnings)
    if blockers:
        payload_preview["blockers"] = _unique_strings(blockers)

    return OutcomeLifecycleCandidate(
        source=row_source,
        candidate_id=candidate_id,
        symbol=symbol,
        timeframe=_first_text(data, ("timeframe", "time_frame", "tf")),
        strategy_mode=_first_text(data, ("strategy_mode", "setup_mode", "mode")),
        direction=_lowercase(_first_text(data, ("direction", "side", "bias"))),
        lifecycle_status=lifecycle_status,
        normalized_lifecycle_status=normalized_status,
        mapped_outcome_status=outcome_status,
        mapped_terminal_reason=terminal_reason,
        is_terminal_lifecycle=is_terminal,
        is_open_lifecycle=is_open,
        is_negative_example=is_negative,
        is_outcome_event_eligible=not severe_blockers,
        payload_preview=_jsonable(payload_preview),
        warnings=tuple(_unique_strings(warnings)),
        blockers=tuple(_unique_strings(blockers)),
    )


def _mapping_for(status: Any, normalized_status: Any = NA) -> OutcomeLifecycleMapping:
    status_text = _text(status)
    normalized_text = _text(normalized_status)
    for candidate_status in (status_text, normalized_text):
        outcome_status = _outcome_status_for(candidate_status)
        if outcome_status != "UNKNOWN":
            return OutcomeLifecycleMapping(
                lifecycle_status=status_text,
                normalized_lifecycle_status=normalized_text,
                mapped_outcome_status=outcome_status,
                mapped_terminal_reason=TERMINAL_REASON_BY_OUTCOME_STATUS[outcome_status],
            )

    return OutcomeLifecycleMapping(
        lifecycle_status=status_text,
        normalized_lifecycle_status=normalized_text,
        mapped_outcome_status="UNKNOWN",
        mapped_terminal_reason="unknown",
    )


def _outcome_status_for(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return "UNKNOWN"
    key = _status_key(text)
    if not key:
        return "UNKNOWN"
    outcome_status = OUTCOME_STATUS_BY_STATUS_KEY.get(key)
    if outcome_status is not None:
        return outcome_status
    upper = key.upper()
    if upper in TERMINAL_OUTCOME_STATUSES | OPEN_OUTCOME_STATUSES | NEGATIVE_OUTCOME_STATUSES | UNKNOWN_OUTCOME_STATUSES:
        return upper
    return "UNKNOWN"


def _make_result(
    *,
    source: str,
    candidates: Sequence[OutcomeLifecycleCandidate],
    issues: Sequence[OutcomeLifecycleIssue],
) -> OutcomeLifecycleIntegrationResult:
    candidate_tuple = tuple(candidates)
    candidate_issues = tuple(
        issue
        for index, candidate in enumerate(candidate_tuple)
        for issue in _issues_for_candidate(candidate, candidate_index=index)
    )
    all_issues = tuple(issues) + candidate_issues
    warning_count = sum(1 for issue in all_issues if issue.severity == "warning")
    error_count = sum(1 for issue in all_issues if issue.severity == "error")
    blocker_counts = Counter(blocker for candidate in candidate_tuple for blocker in candidate.blockers)
    total = len(candidate_tuple)

    summary = OutcomeLifecycleIntegrationSummary(
        total_candidates=total,
        eligible_candidates=sum(1 for candidate in candidate_tuple if candidate.is_outcome_event_eligible),
        ineligible_candidates=sum(1 for candidate in candidate_tuple if not candidate.is_outcome_event_eligible),
        terminal_candidates=sum(1 for candidate in candidate_tuple if candidate.is_terminal_lifecycle),
        open_candidates=sum(1 for candidate in candidate_tuple if candidate.is_open_lifecycle),
        negative_example_candidates=sum(1 for candidate in candidate_tuple if candidate.is_negative_example),
        unknown_status_candidates=sum(1 for candidate in candidate_tuple if candidate.mapped_outcome_status == "UNKNOWN"),
        mapped_outcome_status_counts=dict(sorted(Counter(candidate.mapped_outcome_status for candidate in candidate_tuple).items())),
        mapped_terminal_reason_counts=dict(sorted(Counter(candidate.mapped_terminal_reason for candidate in candidate_tuple).items())),
        blocker_counts=dict(sorted(blocker_counts.items())),
        warning_count=warning_count,
        error_count=error_count,
        is_valid=error_count == 0,
    )
    return OutcomeLifecycleIntegrationResult(
        source=_text(source),
        summary=summary,
        candidates=candidate_tuple,
        issues=all_issues,
    )


def _issues_for_candidate(
    candidate: OutcomeLifecycleCandidate,
    *,
    candidate_index: int,
) -> tuple[OutcomeLifecycleIssue, ...]:
    issues: list[OutcomeLifecycleIssue] = []
    for blocker in candidate.blockers:
        issues.append(
            OutcomeLifecycleIssue(
                severity="blocker",
                code=_issue_code_from_message(blocker),
                message=blocker,
                path=f"candidates[{candidate_index}].blockers",
                candidate_id=candidate.candidate_id,
            )
        )
    for warning in candidate.warnings:
        issues.append(
            OutcomeLifecycleIssue(
                severity="warning",
                code=_issue_code_from_message(warning),
                message=warning,
                path=f"candidates[{candidate_index}].warnings",
                candidate_id=candidate.candidate_id,
            )
        )
    return tuple(issues)


def _issue_from_replay_validation_issue(issue: object) -> OutcomeLifecycleIssue:
    severity = _issue_severity(issue)
    if severity == "blocker":
        severity = "warning"
    return OutcomeLifecycleIssue(
        severity=severity,
        code=f"replay_validation_{_text(getattr(issue, 'code', 'issue'))}",
        message=_text(getattr(issue, "message", issue)),
        path=_text(getattr(issue, "path", "replay_validation")),
    )


def _issue_severity(issue: object) -> IssueSeverity | Literal["info"]:
    severity = _text(getattr(issue, "severity", "warning")).lower()
    if severity in {"warning", "blocker", "error", "info"}:
        return severity  # type: ignore[return-value]
    return "warning"


def _issue_code_from_message(message: str) -> str:
    lowered = _status_key(message)
    if "symbol_missing" in lowered or lowered.startswith("symbol_missing"):
        return "symbol_missing"
    if "status_information_missing" in lowered or "status_information" in lowered:
        return "status_missing"
    if "unknown" in lowered:
        return "unknown_lifecycle_status"
    if "timestamp" in lowered or "closed_at" in lowered:
        return "terminal_timestamp_missing"
    if "exit_price" in lowered or "resolved_price" in lowered:
        return "exit_price_missing"
    if "terminal_reason" in lowered:
        return "terminal_reason_missing"
    if "inspected" in lowered:
        return "invalid_candidate_row"
    return lowered[:80] or "outcome_lifecycle_issue"


def _payload_value_for_field(field_name: str, data: Mapping[str, Any]) -> str:
    aliases = {
        "candidate_id": ("candidate_id",),
        "symbol": ("symbol", "ticker", "market"),
        "exchange": ("exchange", "exchange_name"),
        "market_type": ("market_type",),
        "timeframe": ("timeframe", "time_frame", "tf"),
        "strategy_name": ("strategy_name", "strategy"),
        "strategy_mode": ("strategy_mode", "setup_mode", "mode"),
        "direction": ("direction", "side", "bias"),
        "setup_id": ("setup_id", "setup_fingerprint", "lifecycle_id"),
        "trade_idea_id": ("trade_idea_id", "idea_id"),
        "alert_id": ("alert_id",),
        "run_id": ("run_id", "scan_run_id"),
        "scan_id": ("scan_id",),
        "outcome_timestamp": ("outcome_timestamp", "terminal_timestamp"),
        "closed_at": ("closed_at",),
        "exit_price": ("exit_price",),
        "resolved_price": ("resolved_price",),
        "result_r": ("result_r", "final_r", "final_r_multiple", "r_multiple"),
    }
    return _first_text(data, aliases[field_name])


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if isinstance(row, Mapping):
        return {str(key): _jsonable(value) for key, value in row.items()}
    if isinstance(row, ReplayValidationCandidate):
        return {field_info.name: _jsonable(getattr(row, field_info.name)) for field_info in fields(ReplayValidationCandidate)}
    if is_dataclass(row):
        return {str(key): _jsonable(value) for key, value in asdict(row).items()}
    model_dump = getattr(row, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return {str(key): _jsonable(value) for key, value in dumped.items()}
    return None


def _candidate_id(data: Mapping[str, Any], *, source: str, row_index: int) -> str:
    explicit = _first_text(data, ("candidate_id",))
    if explicit != NA:
        return explicit
    stable_values = [_text(data.get(field_name)) for field_name in STABLE_IDENTITY_FIELDS]
    basis = [_text(source)]
    if any(_present(value) for value in stable_values):
        basis.extend(stable_values)
    else:
        basis.extend(
            [
                str(row_index),
                _first_text(data, ("symbol", "ticker", "market")),
                _first_text(data, ("timeframe", "time_frame", "tf")),
                _first_text(data, STATUS_FIELDS),
                _first_text(data, NORMALIZED_STATUS_FIELDS),
            ]
        )
    digest = hashlib.sha256("\x1f".join(basis).encode("utf-8")).hexdigest()[:16]
    return f"ol-{digest}"


def _first_text(data: Mapping[str, Any], field_names: Sequence[str]) -> str:
    for field_name in field_names:
        if field_name not in data:
            continue
        text = _scalar_text(data.get(field_name))
        if text != NA:
            return text
    return NA


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text != NA:
            return text
    return NA


def _has_any_present(data: Mapping[str, Any], field_names: Sequence[str]) -> bool:
    return any(_present(_first_text(data, (field_name,))) for field_name in field_names)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, Mapping))


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


def _scalar_text(value: Any) -> str:
    if isinstance(value, Mapping) or _is_sequence(value):
        return NA
    return _text(value)


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
    "OUTCOME_LIFECYCLE_INTEGRATION_SCHEMA_VERSION",
    "OutcomeLifecycleCandidate",
    "OutcomeLifecycleIntegrationResult",
    "OutcomeLifecycleIntegrationSummary",
    "OutcomeLifecycleIssue",
    "OutcomeLifecycleMapping",
    "audit_outcome_lifecycle_candidate",
    "audit_outcome_lifecycle_candidates",
    "audit_outcome_lifecycle_from_files",
    "build_outcome_event_payload_from_candidate",
    "map_lifecycle_status_to_outcome_status",
    "map_lifecycle_status_to_terminal_reason",
    "outcome_lifecycle_result_to_dict",
]
