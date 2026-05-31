from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.analytics.replay_dataset_export import ReplayDatasetRow, export_replay_dataset_from_files

NA = "N/A"
REPLAY_OUTCOME_READINESS_SCHEMA_VERSION = "replay_outcome_readiness_v1"

REPLAY_OUTCOME_READINESS_SAFETY_NOTE = (
    "Replay outcome readiness is a read-only field-contract audit. It does not mutate artifacts, "
    "lifecycle state, scanner results, setup gates, alerts, trade ideas, market data, or trading actions."
)

IssueSeverity = Literal["warning", "error"]
RequirementCategory = Literal["identity", "trade_setup", "terminal_outcome", "negative_example"]
RequirementMode = Literal["all", "any_of", "entry_or_zone"]

STABLE_IDENTITY_FIELDS = ("run_id", "scan_id", "setup_id", "trade_idea_id", "alert_id")
IDENTITY_FIELD_KEYS = ("symbol", "timeframe", "source", "status", "stable_identifier")
TRADE_FIELD_KEYS = (
    "direction",
    "entry_or_entry_zone",
    "stop_or_invalidation",
    "tp_target",
    "strategy_name",
    "strategy_mode",
    "trade_timestamp",
)
TERMINAL_FIELD_KEYS = (
    "outcome_status",
    "terminal_timestamp",
    "result_r",
    "exit_price",
    "terminal_reason",
)
NEGATIVE_EXAMPLE_FIELD_KEYS = ("rejection_reason_or_first_failed_gate", "status", "symbol", "negative_timestamp")
TIMESTAMP_FIELD_KEYS = {"trade_timestamp", "terminal_timestamp", "negative_timestamp"}

STATUS_FIELDS = ("status", "lifecycle_status", "current_state", "state", "display_status", "readiness_label", "last_status")
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
TRADE_TIMESTAMP_FIELDS = ("scan_timestamp", "timestamp", "event_timestamp")
TERMINAL_TIMESTAMP_FIELDS = ("terminal_timestamp", "closed_at", "outcome_timestamp")
EXIT_PRICE_FIELDS = ("exit_price", "resolved_price")
TERMINAL_REASON_FIELDS = ("terminal_reason", "outcome_reason", "close_reason", "exit_reason")

NEGATIVE_STATUS_KEYS = {"no_setup", "rejected", "scan_error", "scanned_no_setup"}
TRADE_STATUS_KEYS = {
    "alert_created",
    "alert_dry_run_created",
    "alert_sent",
    "confirmed",
    "executing",
    "idea_created",
    "journal_entry_created",
    "trade_idea_created",
    "triggered",
    "valid_setup",
}
TERMINAL_STATUS_KEYS = {
    "cancelled",
    "canceled",
    "closed",
    "cooldown",
    "expired",
    "invalidated",
    "sl",
    "sl_hit",
    "stop_loss_hit",
    "stopped",
    "take_profit_hit",
    "terminal_sl",
    "terminal_tp",
    "tp",
    "tp_hit",
    "tp1",
    "tp1_hit",
    "tp2",
    "tp2_hit",
    "tp3",
    "tp3_hit",
}


@dataclass(frozen=True)
class ReplayOutcomeFieldRequirement:
    category: RequirementCategory
    field_key: str
    fields: tuple[str, ...]
    mode: RequirementMode = "all"
    present: bool = False
    severity: IssueSeverity = "warning"
    message: str = NA


@dataclass(frozen=True)
class ReplayOutcomeReadinessIssue:
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"
    candidate_id: str = NA
    field_key: str = NA
    category: str = NA


@dataclass(frozen=True)
class ReplayOutcomeReadinessCandidate:
    schema_version: str = REPLAY_OUTCOME_READINESS_SCHEMA_VERSION
    source: str = NA
    row_index: int = 0
    candidate_id: str = NA
    symbol: str = NA
    timeframe: str = NA
    status: str = NA
    normalized_lifecycle_status: str = NA
    run_id: str = NA
    scan_id: str = NA
    setup_id: str = NA
    trade_idea_id: str = NA
    alert_id: str = NA
    strategy_name: str = NA
    strategy_mode: str = NA
    direction: str = NA
    scan_timestamp: str = NA
    event_timestamp: str = NA
    entry: str = NA
    entry_low: str = NA
    entry_high: str = NA
    stop: str = NA
    invalidation: str = NA
    tp1: str = NA
    tp2: str = NA
    tp3: str = NA
    outcome_status: str = NA
    terminal_timestamp: str = NA
    closed_at: str = NA
    outcome_timestamp: str = NA
    result_r: str = NA
    exit_price: str = NA
    resolved_price: str = NA
    terminal_reason: str = NA
    rejection_reason: str = NA
    first_failed_gate: str = NA
    identity_ready: bool = False
    trade_like_candidate: bool = False
    trade_contract_ready: bool = False
    terminal_candidate: bool = False
    terminal_contract_ready: bool = False
    negative_example_candidate: bool = False
    negative_example_ready: bool = False
    outcome_ready: bool = False
    missing_fields: tuple[str, ...] = ()
    outcome_readiness_blockers: tuple[str, ...] = ()
    outcome_readiness_warnings: tuple[str, ...] = ()
    field_requirements: tuple[ReplayOutcomeFieldRequirement, ...] = ()


@dataclass(frozen=True)
class ReplayOutcomeReadinessSummary:
    total_candidates: int = 0
    outcome_ready_candidates: int = 0
    outcome_not_ready_candidates: int = 0
    outcome_ready_rate: float = 0.0
    trade_like_candidates: int = 0
    terminal_candidates: int = 0
    negative_example_candidates: int = 0
    identity_ready_candidates: int = 0
    missing_identity_count: int = 0
    missing_timestamp_count: int = 0
    missing_trade_field_count: int = 0
    missing_terminal_field_count: int = 0
    missing_negative_example_field_count: int = 0
    field_missing_counts: dict[str, int] = field(default_factory=dict)
    blocker_counts: dict[str, int] = field(default_factory=dict)
    warning_count: int = 0
    error_count: int = 0
    is_valid: bool = True


@dataclass(frozen=True)
class ReplayOutcomeReadinessResult:
    source: str
    schema_version: str = REPLAY_OUTCOME_READINESS_SCHEMA_VERSION
    summary: ReplayOutcomeReadinessSummary = field(default_factory=ReplayOutcomeReadinessSummary)
    candidates: tuple[ReplayOutcomeReadinessCandidate, ...] = ()
    issues: tuple[ReplayOutcomeReadinessIssue, ...] = ()
    safety_note: str = REPLAY_OUTCOME_READINESS_SAFETY_NOTE


def required_outcome_fields_for_candidate(candidate_or_row: Any) -> list[ReplayOutcomeFieldRequirement]:
    if isinstance(candidate_or_row, ReplayOutcomeReadinessCandidate):
        return list(candidate_or_row.field_requirements)
    candidate = _candidate_from_row(candidate_or_row, row_index=0, source="in_memory")
    return list(candidate.field_requirements)


def audit_replay_outcome_readiness(
    candidates_or_rows: list[Any],
    source: str = "in_memory",
) -> ReplayOutcomeReadinessResult:
    if not _is_sequence(candidates_or_rows):
        issue = ReplayOutcomeReadinessIssue(
            severity="error",
            code="invalid_rows_input",
            message="Replay outcome readiness input must be a sequence of replay rows or candidates.",
        )
        return _make_result(source=source, candidates=(), issues=(issue,))

    issues: list[ReplayOutcomeReadinessIssue] = []
    candidates: list[ReplayOutcomeReadinessCandidate] = []
    for row_index, row in enumerate(candidates_or_rows):
        if isinstance(row, ReplayOutcomeReadinessCandidate):
            candidates.append(row)
            continue
        data = _row_to_dict(row)
        if data is None:
            issues.append(
                ReplayOutcomeReadinessIssue(
                    severity="warning",
                    code="unsupported_candidate_row",
                    message="Replay outcome readiness row could not be inspected as a mapping or dataclass.",
                    path=f"candidates[{row_index}]",
                )
            )
        candidates.append(_candidate_from_row(data or {}, row_index=row_index, source=source))

    return _make_result(source=source, candidates=tuple(candidates), issues=tuple(issues))


def audit_replay_outcome_readiness_from_files(paths: list[Path]) -> ReplayOutcomeReadinessResult:
    export_result = export_replay_dataset_from_files(paths)
    source = ", ".join(export_result.summary.sources) if export_result.summary.sources else "files"
    result = audit_replay_outcome_readiness(list(export_result.rows), source=source)

    issues: list[ReplayOutcomeReadinessIssue] = []
    if export_result.warnings:
        issues.append(
            ReplayOutcomeReadinessIssue(
                severity="warning",
                code="export_warnings_present",
                message=f"Replay export reported {len(export_result.warnings)} warning(s).",
                path="export_result.warnings",
            )
        )
    for index, message in enumerate(export_result.errors):
        issues.append(
            ReplayOutcomeReadinessIssue(
                severity="error",
                code="export_error",
                message=str(message),
                path=f"export_result.errors[{index}]",
            )
        )
    if not issues:
        return result
    return _make_result(source=result.source, candidates=result.candidates, issues=tuple(issues) + result.issues)


def replay_outcome_readiness_result_to_dict(result: ReplayOutcomeReadinessResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def _candidate_from_row(data_or_row: Any, *, row_index: int, source: str) -> ReplayOutcomeReadinessCandidate:
    data = _row_to_dict(data_or_row) or {}
    row_source = _first_non_na(_text(data.get("source")), _text(source))
    status = _first_text(data, STATUS_FIELDS)
    normalized_status = _first_text(data, ("normalized_lifecycle_status", "normalized_status"))
    scan_timestamp = _first_text(data, TIMESTAMP_FIELDS)
    candidate_id = _candidate_id(data, source=row_source, row_index=row_index)

    candidate = ReplayOutcomeReadinessCandidate(
        source=row_source,
        row_index=row_index,
        candidate_id=candidate_id,
        symbol=_uppercase(_first_text(data, ("symbol", "ticker", "market"))),
        timeframe=_first_text(data, ("timeframe", "time_frame", "tf")),
        status=status,
        normalized_lifecycle_status=normalized_status,
        run_id=_first_text(data, ("run_id", "scan_run_id")),
        scan_id=_first_text(data, ("scan_id",)),
        setup_id=_first_text(data, ("setup_id", "setup_fingerprint", "lifecycle_id")),
        trade_idea_id=_first_text(data, ("trade_idea_id", "idea_id")),
        alert_id=_first_text(data, ("alert_id",)),
        strategy_name=_first_text(data, ("strategy_name", "strategy")),
        strategy_mode=_first_text(data, ("strategy_mode", "setup_mode", "mode")),
        direction=_lowercase(_first_text(data, ("direction", "side", "bias"))),
        scan_timestamp=scan_timestamp,
        event_timestamp=_first_text(data, ("event_timestamp", "timestamp")),
        entry=_first_text(data, ("entry", "entry_price", "entry_trigger")),
        entry_low=_first_text(data, ("entry_low",)),
        entry_high=_first_text(data, ("entry_high",)),
        stop=_first_text(data, ("stop", "stop_loss", "stop_price")),
        invalidation=_first_text(data, ("invalidation", "cancel_condition")),
        tp1=_first_text(data, ("tp1", "target_1", "take_profit_1")),
        tp2=_first_text(data, ("tp2", "target_2", "take_profit_2")),
        tp3=_first_text(data, ("tp3", "target_3", "take_profit_3")),
        outcome_status=_first_text(data, ("outcome_status", "outcome", "result")),
        terminal_timestamp=_first_text(data, ("terminal_timestamp",)),
        closed_at=_first_text(data, ("closed_at",)),
        outcome_timestamp=_first_text(data, ("outcome_timestamp",)),
        result_r=_first_text(data, ("result_r", "final_r", "final_r_multiple", "r_multiple")),
        exit_price=_first_text(data, ("exit_price",)),
        resolved_price=_first_text(data, ("resolved_price",)),
        terminal_reason=_first_text(data, TERMINAL_REASON_FIELDS),
        rejection_reason=_first_rejection_reason(data),
        first_failed_gate=_first_text(data, ("first_failed_gate", "failed_gate", "rejection_stage")),
    )
    requirements = tuple(_requirements_for(candidate))
    missing = tuple(requirement for requirement in requirements if not requirement.present)
    blockers, warnings = _readiness_findings(candidate, missing)
    identity_ready = not any(requirement.category == "identity" and requirement.severity == "warning" for requirement in missing)
    trade_like = _is_trade_like_candidate(candidate, data)
    terminal = _is_terminal_candidate(candidate)
    negative = _is_negative_example_candidate(candidate)
    trade_ready = trade_like and not any(requirement.category == "trade_setup" for requirement in missing)
    terminal_ready = terminal and not any(requirement.category == "terminal_outcome" for requirement in missing)
    negative_ready = negative and not any(
        requirement.category == "negative_example" and requirement.field_key != "negative_timestamp"
        for requirement in missing
    )
    outcome_ready = identity_ready and trade_ready and terminal_ready

    return ReplayOutcomeReadinessCandidate(
        **{
            **{field_info.name: getattr(candidate, field_info.name) for field_info in fields(ReplayOutcomeReadinessCandidate)},
            "identity_ready": identity_ready,
            "trade_like_candidate": trade_like,
            "trade_contract_ready": trade_ready,
            "terminal_candidate": terminal,
            "terminal_contract_ready": terminal_ready,
            "negative_example_candidate": negative,
            "negative_example_ready": negative_ready,
            "outcome_ready": outcome_ready,
            "missing_fields": tuple(_unique_strings([requirement.field_key for requirement in missing])),
            "outcome_readiness_blockers": tuple(blockers),
            "outcome_readiness_warnings": tuple(warnings),
            "field_requirements": requirements,
        }
    )


def _requirements_for(candidate: ReplayOutcomeReadinessCandidate) -> list[ReplayOutcomeFieldRequirement]:
    requirements = [
        _requirement("identity", "symbol", ("symbol",), _present(candidate.symbol), message="symbol missing."),
        _requirement("identity", "timeframe", ("timeframe",), _present(candidate.timeframe), message="timeframe missing."),
        _requirement("identity", "source", ("source",), _present(candidate.source), message="source missing."),
        _requirement(
            "identity",
            "status",
            ("status", "normalized_lifecycle_status"),
            _present(candidate.status) or _present(candidate.normalized_lifecycle_status),
            mode="any_of",
            message="status missing.",
        ),
        _requirement(
            "identity",
            "stable_identifier",
            (*STABLE_IDENTITY_FIELDS, "candidate_id"),
            _has_any(candidate, (*STABLE_IDENTITY_FIELDS, "candidate_id")),
            mode="any_of",
            message="stable identifier missing.",
        ),
    ]

    if _is_trade_like_candidate(candidate, {}):
        requirements.extend(
            [
                _requirement("trade_setup", "direction", ("direction",), _present(candidate.direction), message="direction missing for trade-like row."),
                _requirement(
                    "trade_setup",
                    "entry_or_entry_zone",
                    ("entry", "entry_low", "entry_high"),
                    _has_entry_context(candidate),
                    mode="entry_or_zone",
                    message="entry or entry zone missing for trade-like row.",
                ),
                _requirement(
                    "trade_setup",
                    "stop_or_invalidation",
                    ("stop", "invalidation"),
                    _present(candidate.stop) or _present(candidate.invalidation),
                    mode="any_of",
                    message="stop or invalidation missing for trade-like row.",
                ),
                _requirement(
                    "trade_setup",
                    "tp_target",
                    ("tp1", "tp2", "tp3"),
                    _present(candidate.tp1) or _present(candidate.tp2) or _present(candidate.tp3),
                    mode="any_of",
                    message="tp target missing for trade-like row.",
                ),
                _requirement("trade_setup", "strategy_name", ("strategy_name",), _present(candidate.strategy_name), message="strategy_name missing for trade-like row."),
                _requirement("trade_setup", "strategy_mode", ("strategy_mode",), _present(candidate.strategy_mode), message="strategy_mode missing for trade-like row."),
                _requirement(
                    "trade_setup",
                    "trade_timestamp",
                    TRADE_TIMESTAMP_FIELDS,
                    _has_any(candidate, TRADE_TIMESTAMP_FIELDS),
                    mode="any_of",
                    message="timestamp missing for trade-like row.",
                ),
            ]
        )

    if _is_terminal_candidate(candidate):
        requirements.extend(
            [
                _requirement("terminal_outcome", "outcome_status", ("outcome_status",), _present(candidate.outcome_status), message="outcome_status missing for terminal row."),
                _requirement(
                    "terminal_outcome",
                    "terminal_timestamp",
                    TERMINAL_TIMESTAMP_FIELDS,
                    _has_any(candidate, TERMINAL_TIMESTAMP_FIELDS),
                    mode="any_of",
                    message="terminal timestamp missing for terminal row.",
                ),
                _requirement("terminal_outcome", "result_r", ("result_r",), _present(candidate.result_r), message="result_r missing for terminal row."),
                _requirement(
                    "terminal_outcome",
                    "exit_price",
                    EXIT_PRICE_FIELDS,
                    _has_any(candidate, EXIT_PRICE_FIELDS),
                    mode="any_of",
                    message="exit_price or resolved_price missing for terminal row.",
                ),
                _requirement("terminal_outcome", "terminal_reason", ("terminal_reason",), _present(candidate.terminal_reason), message="terminal_reason missing for terminal row."),
            ]
        )

    if _is_negative_example_candidate(candidate):
        requirements.extend(
            [
                _requirement(
                    "negative_example",
                    "rejection_reason_or_first_failed_gate",
                    ("rejection_reason", "first_failed_gate"),
                    _present(candidate.rejection_reason) or _present(candidate.first_failed_gate),
                    mode="any_of",
                    message="rejection_reason or first_failed_gate missing for negative example row.",
                ),
                _requirement("negative_example", "status", ("status",), _present(candidate.status), message="status missing for negative example row."),
                _requirement("negative_example", "symbol", ("symbol",), _present(candidate.symbol), message="symbol missing for negative example row."),
                _requirement(
                    "negative_example",
                    "negative_timestamp",
                    ("scan_timestamp", "event_timestamp"),
                    _has_any(candidate, ("scan_timestamp", "event_timestamp")),
                    mode="any_of",
                    message="timestamp missing for negative example row; explicit warning recorded.",
                ),
            ]
        )
    return requirements


def _requirement(
    category: RequirementCategory,
    field_key: str,
    fields: tuple[str, ...],
    present: bool,
    *,
    mode: RequirementMode = "all",
    message: str,
) -> ReplayOutcomeFieldRequirement:
    return ReplayOutcomeFieldRequirement(
        category=category,
        field_key=field_key,
        fields=fields,
        mode=mode,
        present=present,
        severity="warning",
        message=message,
    )


def _readiness_findings(
    candidate: ReplayOutcomeReadinessCandidate,
    missing: Sequence[ReplayOutcomeFieldRequirement],
) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []

    if not any(_present(getattr(candidate, field_name)) for field_name in STABLE_IDENTITY_FIELDS):
        warnings.append("artifact stable identifier missing; deterministic candidate_id fallback used.")

    for requirement in missing:
        if requirement.field_key in TIMESTAMP_FIELD_KEYS or (
            requirement.category == "identity" and requirement.field_key == "stable_identifier"
        ):
            warnings.append(requirement.message)
        else:
            blockers.append(requirement.message)
    return _unique_strings(blockers), _unique_strings(warnings)


def _make_result(
    *,
    source: str,
    candidates: Sequence[ReplayOutcomeReadinessCandidate],
    issues: Sequence[ReplayOutcomeReadinessIssue],
) -> ReplayOutcomeReadinessResult:
    candidate_tuple = tuple(candidates)
    candidate_issues = tuple(_issues_for_candidate(candidate, index) for index, candidate in enumerate(candidate_tuple))
    flattened_candidate_issues = tuple(issue for issue_group in candidate_issues for issue in issue_group)
    all_issues = tuple(issues) + flattened_candidate_issues
    warning_count = sum(1 for issue in all_issues if issue.severity == "warning")
    error_count = sum(1 for issue in all_issues if issue.severity == "error")
    total = len(candidate_tuple)
    ready_count = sum(1 for candidate in candidate_tuple if candidate.outcome_ready)
    missing_by_key = Counter(
        field_key
        for candidate in candidate_tuple
        for field_key in candidate.missing_fields
    )
    blocker_counts = Counter(
        blocker
        for candidate in candidate_tuple
        for blocker in candidate.outcome_readiness_blockers
    )

    summary = ReplayOutcomeReadinessSummary(
        total_candidates=total,
        outcome_ready_candidates=ready_count,
        outcome_not_ready_candidates=total - ready_count,
        outcome_ready_rate=_rate(ready_count, total),
        trade_like_candidates=sum(1 for candidate in candidate_tuple if candidate.trade_like_candidate),
        terminal_candidates=sum(1 for candidate in candidate_tuple if candidate.terminal_candidate),
        negative_example_candidates=sum(1 for candidate in candidate_tuple if candidate.negative_example_candidate),
        identity_ready_candidates=sum(1 for candidate in candidate_tuple if candidate.identity_ready),
        missing_identity_count=sum(1 for candidate in candidate_tuple if not candidate.identity_ready),
        missing_timestamp_count=sum(
            1 for candidate in candidate_tuple if any(field_key in TIMESTAMP_FIELD_KEYS for field_key in candidate.missing_fields)
        ),
        missing_trade_field_count=_missing_count_by_category(candidate_tuple, "trade_setup"),
        missing_terminal_field_count=_missing_count_by_category(candidate_tuple, "terminal_outcome"),
        missing_negative_example_field_count=_missing_count_by_category(candidate_tuple, "negative_example"),
        field_missing_counts=dict(sorted(missing_by_key.items())),
        blocker_counts=dict(sorted(blocker_counts.items())),
        warning_count=warning_count,
        error_count=error_count,
        is_valid=error_count == 0,
    )
    return ReplayOutcomeReadinessResult(
        source=_text(source),
        summary=summary,
        candidates=candidate_tuple,
        issues=all_issues,
    )


def _issues_for_candidate(
    candidate: ReplayOutcomeReadinessCandidate,
    candidate_index: int,
) -> tuple[ReplayOutcomeReadinessIssue, ...]:
    issues: list[ReplayOutcomeReadinessIssue] = []
    missing_by_message = {requirement.message: requirement for requirement in candidate.field_requirements}
    for blocker in candidate.outcome_readiness_blockers:
        requirement = missing_by_message.get(blocker)
        issues.append(
            ReplayOutcomeReadinessIssue(
                severity="warning",
                code="outcome_readiness_blocker",
                message=blocker,
                path=f"candidates[{candidate_index}]",
                candidate_id=candidate.candidate_id,
                field_key=requirement.field_key if requirement else NA,
                category=requirement.category if requirement else NA,
            )
        )
    for warning in candidate.outcome_readiness_warnings:
        requirement = missing_by_message.get(warning)
        issues.append(
            ReplayOutcomeReadinessIssue(
                severity="warning",
                code="outcome_readiness_warning",
                message=warning,
                path=f"candidates[{candidate_index}]",
                candidate_id=candidate.candidate_id,
                field_key=requirement.field_key if requirement else NA,
                category=requirement.category if requirement else NA,
            )
        )
    return tuple(issues)


def _missing_count_by_category(
    candidates: Sequence[ReplayOutcomeReadinessCandidate],
    category: RequirementCategory,
) -> int:
    return sum(
        1
        for candidate in candidates
        for requirement in candidate.field_requirements
        if requirement.category == category and not requirement.present
    )


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if isinstance(row, Mapping):
        return {str(key): _jsonable(value) for key, value in row.items()}
    if isinstance(row, ReplayDatasetRow):
        return {field_info.name: _jsonable(getattr(row, field_info.name)) for field_info in fields(ReplayDatasetRow)}
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
    stable_values = [_first_text(data, ("row_id",)), *[_text(data.get(field_name)) for field_name in STABLE_IDENTITY_FIELDS]]
    basis = [source]
    if any(_present(value) for value in stable_values):
        basis.extend(stable_values)
    else:
        basis.extend(
            [
                str(row_index),
                _first_text(data, ("symbol", "ticker", "market")),
                _first_text(data, ("timeframe", "time_frame", "tf")),
                _first_text(data, STATUS_FIELDS),
                _first_text(data, TIMESTAMP_FIELDS),
            ]
        )
    digest = hashlib.sha256("\x1f".join(basis).encode("utf-8")).hexdigest()[:16]
    return f"or-{digest}"


def _is_negative_example_candidate(candidate: ReplayOutcomeReadinessCandidate) -> bool:
    return any(_status_key(value) in NEGATIVE_STATUS_KEYS for value in (candidate.status, candidate.normalized_lifecycle_status))


def _is_trade_like_candidate(candidate: ReplayOutcomeReadinessCandidate, data: Mapping[str, Any]) -> bool:
    if _is_negative_example_candidate(candidate):
        return False
    if _truthy(data.get("trade_idea_present")) or _truthy(data.get("alert_present")) or _truthy(data.get("journal_entry_present")):
        return True
    if _has_any(candidate, ("setup_id", "trade_idea_id", "alert_id")):
        return True
    if any(_status_key(value) in TRADE_STATUS_KEYS | TERMINAL_STATUS_KEYS for value in (candidate.status, candidate.normalized_lifecycle_status)):
        return True
    return any(
        _present(value)
        for value in (
            candidate.entry,
            candidate.entry_low,
            candidate.entry_high,
            candidate.stop,
            candidate.invalidation,
            candidate.tp1,
            candidate.tp2,
            candidate.tp3,
        )
    )


def _is_terminal_candidate(candidate: ReplayOutcomeReadinessCandidate) -> bool:
    if any(_status_key(value) in TERMINAL_STATUS_KEYS for value in (candidate.status, candidate.normalized_lifecycle_status)):
        return True
    return any(
        _present(value)
        for value in (
            candidate.outcome_status,
            candidate.terminal_timestamp,
            candidate.closed_at,
            candidate.outcome_timestamp,
            candidate.result_r,
            candidate.exit_price,
            candidate.resolved_price,
            candidate.terminal_reason,
        )
    )


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


def _has_any(candidate: ReplayOutcomeReadinessCandidate, field_names: Sequence[str]) -> bool:
    return any(_present(getattr(candidate, field_name, NA)) for field_name in field_names)


def _has_entry_context(candidate: ReplayOutcomeReadinessCandidate) -> bool:
    return _present(candidate.entry) or (_present(candidate.entry_low) and _present(candidate.entry_high))


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
    "REPLAY_OUTCOME_READINESS_SCHEMA_VERSION",
    "ReplayOutcomeFieldRequirement",
    "ReplayOutcomeReadinessCandidate",
    "ReplayOutcomeReadinessIssue",
    "ReplayOutcomeReadinessResult",
    "ReplayOutcomeReadinessSummary",
    "audit_replay_outcome_readiness",
    "audit_replay_outcome_readiness_from_files",
    "replay_outcome_readiness_result_to_dict",
    "required_outcome_fields_for_candidate",
]
