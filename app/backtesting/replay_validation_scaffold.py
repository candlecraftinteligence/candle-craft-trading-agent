from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.analytics.replay_dataset_coverage import classify_lifecycle_bucket, classify_setup_research_bucket
from app.analytics.replay_dataset_export import ReplayDatasetRow, export_replay_dataset_from_files

NA = "N/A"
REPLAY_VALIDATION_SCHEMA_VERSION = "historical_replay_validation_v1"

REPLAY_VALIDATION_SAFETY_NOTE = (
    "Historical replay validation scaffolding is plan-only. It does not execute trades, simulate live trading, "
    "create signals, call exchanges, send Telegram messages, mutate lifecycle state, alter setup gates, "
    "invent market data, or calculate profitability, win rate, edge, or expectancy."
)

IssueSeverity = Literal["info", "warning", "error"]
TimestampStatus = Literal["present", "missing", "N/A"]

STABLE_IDENTITY_FIELDS = ("run_id", "scan_id", "setup_id", "trade_idea_id", "alert_id")
NEGATIVE_LIFECYCLE_BUCKETS = {"no_setup", "rejected", "scan_error"}
NEGATIVE_SETUP_BUCKETS = {"gate_failed", "near_miss", "no_setup", "rejected", "scan_error"}
TRADE_LIFECYCLE_BUCKETS = {"triggered", "confirmed", "executing", "terminal_tp", "terminal_sl", "closed"}
TERMINAL_LIFECYCLE_BUCKETS = {"terminal_tp", "terminal_sl", "closed"}
TERMINAL_STATUS_KEYS = {
    "closed",
    "sl_hit",
    "stop_loss_hit",
    "stopped",
    "tp_hit",
    "tp1_hit",
    "tp2_hit",
    "tp3_hit",
    "take_profit_hit",
}


@dataclass(frozen=True)
class ReplayValidationIssue:
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"


@dataclass(frozen=True)
class ReplayValidationEvent:
    event_index: int
    candidate_id: str
    symbol: str
    timestamp: str
    timestamp_status: TimestampStatus
    status: str
    lifecycle_bucket: str
    setup_research_bucket: str
    event_type: str
    description: str


@dataclass(frozen=True)
class ReplayValidationCandidate:
    schema_version: str = REPLAY_VALIDATION_SCHEMA_VERSION
    source: str = NA
    candidate_id: str = NA
    row_index: int = 0
    symbol: str = NA
    exchange: str = NA
    market_type: str = NA
    timeframe: str = NA
    strategy_name: str = NA
    strategy_mode: str = NA
    direction: str = NA
    status: str = NA
    normalized_lifecycle_status: str = NA
    lifecycle_bucket: str = "unknown"
    setup_research_bucket: str = "unknown"
    scan_timestamp: str = NA
    run_id: str = NA
    scan_id: str = NA
    setup_id: str = NA
    trade_idea_id: str = NA
    alert_id: str = NA
    entry_low: str = NA
    entry_high: str = NA
    entry: str = NA
    stop: str = NA
    invalidation: str = NA
    tp1: str = NA
    tp2: str = NA
    tp3: str = NA
    best_rr: str = NA
    rr_to_tp2: str = NA
    first_failed_gate: str = NA
    rejection_reason: str = NA
    result_r: str = NA
    outcome_status: str = NA
    replay_ready: bool = False
    validation_ready: bool = False
    validation_readiness_warnings: tuple[str, ...] = ()
    validation_blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayValidationTimeline:
    source: str = "in_memory"
    event_count: int = 0
    candidate_count: int = 0
    ordered_events: tuple[ReplayValidationEvent, ...] = ()
    ordering_method: str = "input_order"
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayValidationPlan:
    schema_version: str = REPLAY_VALIDATION_SCHEMA_VERSION
    source: str = "in_memory"
    total_candidates: int = 0
    validation_ready_candidates: int = 0
    validation_not_ready_candidates: int = 0
    validation_ready_rate: float = 0.0
    negative_example_candidates: int = 0
    trade_like_candidates: int = 0
    terminal_outcome_candidates: int = 0
    timeline_event_count: int = 0
    symbol_count: int = 0
    timeframe_counts: dict[str, int] = field(default_factory=dict)
    strategy_mode_counts: dict[str, int] = field(default_factory=dict)
    lifecycle_bucket_counts: dict[str, int] = field(default_factory=dict)
    setup_research_bucket_counts: dict[str, int] = field(default_factory=dict)
    blocker_counts: dict[str, int] = field(default_factory=dict)
    warning_counts: dict[str, int] = field(default_factory=dict)
    warning_count: int = 0
    error_count: int = 0
    is_valid: bool = True
    safety_note: str = REPLAY_VALIDATION_SAFETY_NOTE


@dataclass(frozen=True)
class ReplayValidationResult(ReplayValidationPlan):
    issues: tuple[ReplayValidationIssue, ...] = ()
    timeline: ReplayValidationTimeline = field(default_factory=ReplayValidationTimeline)
    candidates: tuple[ReplayValidationCandidate, ...] = ()


def build_replay_validation_candidates(
    rows: list[Any] | tuple[Any, ...],
    source: str = "in_memory",
) -> list[ReplayValidationCandidate]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray, Mapping)):
        return []

    candidates: list[ReplayValidationCandidate] = []
    for row_index, row in enumerate(rows):
        data = _row_to_dict(row)
        if data is None:
            data = {}
        candidates.append(_candidate_from_row(data, row_index=row_index, source=source))
    return candidates


def build_replay_validation_timeline(
    candidates: list[ReplayValidationCandidate],
    source: str = "in_memory",
) -> ReplayValidationTimeline:
    warnings: list[str] = []
    errors: list[str] = []

    all_timestamps_present = all(_present(candidate.scan_timestamp) for candidate in candidates)
    if candidates and all_timestamps_present:
        ordering_method = "timestamp_then_candidate_id"
        ordered_candidates = sorted(candidates, key=lambda candidate: (candidate.scan_timestamp, candidate.candidate_id))
    else:
        ordering_method = "input_order_missing_timestamps" if candidates else "input_order"
        ordered_candidates = list(candidates)
        if candidates:
            warnings.append("One or more candidate timestamps are missing; preserved input order.")

    ordered_events = tuple(
        _event_from_candidate(candidate, event_index=event_index)
        for event_index, candidate in enumerate(ordered_candidates)
    )
    return ReplayValidationTimeline(
        source=_text(source),
        event_count=len(ordered_events),
        candidate_count=len(candidates),
        ordered_events=ordered_events,
        ordering_method=ordering_method,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def build_replay_validation_plan(
    rows: list[Any] | tuple[Any, ...],
    source: str = "in_memory",
) -> ReplayValidationResult:
    issues: list[ReplayValidationIssue] = []
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray, Mapping)):
        issues.append(
            ReplayValidationIssue(
                severity="error",
                code="invalid_rows_input",
                message="Replay validation input must be a sequence of replay dataset rows.",
            )
        )
        return _make_result(source=source, candidates=(), issues=issues)

    candidates = tuple(build_replay_validation_candidates(tuple(rows), source=source))
    if not candidates:
        issues.append(
            ReplayValidationIssue(
                severity="info",
                code="no_candidates",
                message="No replay validation candidates were produced.",
            )
        )

    for index, candidate in enumerate(candidates):
        for blocker in candidate.validation_blockers:
            issues.append(
                ReplayValidationIssue(
                    severity="warning",
                    code="validation_blocker",
                    message=blocker,
                    path=f"candidates[{index}].validation_blockers",
                )
            )
        for warning in candidate.validation_readiness_warnings:
            issues.append(
                ReplayValidationIssue(
                    severity="warning",
                    code="validation_warning",
                    message=warning,
                    path=f"candidates[{index}].validation_readiness_warnings",
                )
            )

    return _make_result(source=source, candidates=candidates, issues=issues)


def build_replay_validation_plan_from_files(paths: list[Path]) -> ReplayValidationResult:
    export_result = export_replay_dataset_from_files(paths)
    source = ", ".join(export_result.summary.sources) if export_result.summary.sources else "files"
    result = build_replay_validation_plan(tuple(export_result.rows), source=source)

    extra_issues: list[ReplayValidationIssue] = []
    if export_result.warnings:
        extra_issues.append(
            ReplayValidationIssue(
                severity="warning",
                code="export_warnings_present",
                message=f"Replay export reported {len(export_result.warnings)} warning(s).",
                path="export_result.warnings",
            )
        )
    for index, message in enumerate(export_result.errors):
        extra_issues.append(
            ReplayValidationIssue(
                severity="error",
                code="export_error",
                message=str(message),
                path=f"export_result.errors[{index}]",
            )
        )
    if not extra_issues:
        return result
    return _make_result(source=result.source, candidates=result.candidates, issues=tuple(extra_issues) + result.issues)


def replay_validation_result_to_dict(result: ReplayValidationResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def _candidate_from_row(data: Mapping[str, Any], *, row_index: int, source: str) -> ReplayValidationCandidate:
    row_source = _first_non_na(_text(data.get("source")), _text(source))
    status = _text(data.get("status"))
    normalized_status = _text(data.get("normalized_lifecycle_status"))
    lifecycle_bucket = classify_lifecycle_bucket(status, normalized_status)
    setup_bucket = classify_setup_research_bucket(data)
    candidate_id = _candidate_id(data, source=row_source, row_index=row_index)

    candidate = ReplayValidationCandidate(
        source=row_source,
        candidate_id=candidate_id,
        row_index=row_index,
        symbol=_uppercase(_text(data.get("symbol"))),
        exchange=_text(data.get("exchange")),
        market_type=_text(data.get("market_type")),
        timeframe=_text(data.get("timeframe")),
        strategy_name=_text(data.get("strategy_name")),
        strategy_mode=_text(data.get("strategy_mode")),
        direction=_lowercase(_text(data.get("direction"))),
        status=status,
        normalized_lifecycle_status=normalized_status,
        lifecycle_bucket=lifecycle_bucket,
        setup_research_bucket=setup_bucket,
        scan_timestamp=_text(data.get("scan_timestamp")),
        run_id=_text(data.get("run_id")),
        scan_id=_text(data.get("scan_id")),
        setup_id=_text(data.get("setup_id")),
        trade_idea_id=_text(data.get("trade_idea_id")),
        alert_id=_text(data.get("alert_id")),
        entry_low=_text(data.get("entry_low")),
        entry_high=_text(data.get("entry_high")),
        entry=_text(data.get("entry")),
        stop=_text(data.get("stop")),
        invalidation=_text(data.get("invalidation")),
        tp1=_text(data.get("tp1")),
        tp2=_text(data.get("tp2")),
        tp3=_text(data.get("tp3")),
        best_rr=_text(data.get("best_rr")),
        rr_to_tp2=_text(data.get("rr_to_tp2")),
        first_failed_gate=_text(data.get("first_failed_gate")),
        rejection_reason=_first_rejection_reason(data),
        result_r=_text(data.get("result_r")),
        outcome_status=_text(data.get("outcome_status")),
        replay_ready=_truthy(data.get("replay_ready")),
    )
    blockers, warnings = _readiness_findings(candidate, data)
    return replace(
        candidate,
        validation_ready=not blockers,
        validation_blockers=tuple(blockers),
        validation_readiness_warnings=tuple(warnings),
    )


def _readiness_findings(candidate: ReplayValidationCandidate, data: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings = list(_sequence_text(data.get("replay_readiness_warnings")))

    if not _present(candidate.source):
        blockers.append("source missing.")
    if not _present(candidate.symbol):
        blockers.append("symbol missing.")
    if not _present(candidate.timeframe):
        blockers.append("timeframe missing.")
    if not (_present(candidate.status) or _present(candidate.normalized_lifecycle_status)):
        blockers.append("status missing.")

    if not _has_artifact_identity(candidate):
        warnings.append("artifact stable identifier missing; deterministic candidate_id fallback used.")
    if not _present(candidate.scan_timestamp):
        warnings.append("timestamp missing; timeline ordering will use input order.")

    if _is_trade_like_candidate(candidate, data):
        if not _present(candidate.direction):
            blockers.append("direction missing for trade-like row.")
        if not (_present(candidate.stop) or _present(candidate.invalidation)):
            blockers.append("stop or invalidation missing for trade-like row.")
        if not _has_entry_context(candidate):
            blockers.append("entry or entry zone missing for trade-like row.")

    if _is_terminal_candidate(candidate) and not (_present(candidate.outcome_status) or _present(candidate.result_r)):
        blockers.append("outcome_status or result_r missing for terminal outcome row.")

    return _unique_strings(blockers), _unique_strings(warnings)


def _make_result(
    *,
    source: str,
    candidates: Sequence[ReplayValidationCandidate],
    issues: Sequence[ReplayValidationIssue],
) -> ReplayValidationResult:
    candidate_tuple = tuple(candidates)
    timeline = build_replay_validation_timeline(list(candidate_tuple), source=source)
    combined_issues = tuple(issues) + tuple(
        ReplayValidationIssue(
            severity="warning",
            code="timeline_warning",
            message=warning,
            path="timeline.warnings",
        )
        for warning in timeline.warnings
    )
    warning_count = sum(1 for issue in combined_issues if issue.severity == "warning")
    error_count = sum(1 for issue in combined_issues if issue.severity == "error")
    ready_count = sum(1 for candidate in candidate_tuple if candidate.validation_ready)
    total = len(candidate_tuple)
    symbols = {candidate.symbol for candidate in candidate_tuple if _present(candidate.symbol)}

    return ReplayValidationResult(
        source=_text(source),
        total_candidates=total,
        validation_ready_candidates=ready_count,
        validation_not_ready_candidates=total - ready_count,
        validation_ready_rate=_rate(ready_count, total),
        negative_example_candidates=sum(1 for candidate in candidate_tuple if _is_negative_example(candidate)),
        trade_like_candidates=sum(1 for candidate in candidate_tuple if _is_trade_like_candidate(candidate, {})),
        terminal_outcome_candidates=sum(1 for candidate in candidate_tuple if _is_terminal_candidate(candidate)),
        timeline_event_count=timeline.event_count,
        symbol_count=len(symbols),
        timeframe_counts=_candidate_counter(candidate_tuple, "timeframe"),
        strategy_mode_counts=_candidate_counter(candidate_tuple, "strategy_mode"),
        lifecycle_bucket_counts=_candidate_counter(candidate_tuple, "lifecycle_bucket"),
        setup_research_bucket_counts=_candidate_counter(candidate_tuple, "setup_research_bucket"),
        blocker_counts=dict(
            sorted(Counter(blocker for candidate in candidate_tuple for blocker in candidate.validation_blockers).items())
        ),
        warning_counts=dict(
            sorted(
                Counter(
                    warning for candidate in candidate_tuple for warning in candidate.validation_readiness_warnings
                ).items()
            )
        ),
        warning_count=warning_count,
        error_count=error_count,
        is_valid=error_count == 0,
        issues=combined_issues,
        timeline=timeline,
        candidates=candidate_tuple,
    )


def _event_from_candidate(candidate: ReplayValidationCandidate, *, event_index: int) -> ReplayValidationEvent:
    timestamp_present = _present(candidate.scan_timestamp)
    event_type = _event_type(candidate)
    status = _first_non_na(candidate.normalized_lifecycle_status, candidate.status)
    description = (
        f"{event_type} for {candidate.symbol} with status {status}"
        if _present(candidate.symbol)
        else f"{event_type} with status {status}"
    )
    return ReplayValidationEvent(
        event_index=event_index,
        candidate_id=candidate.candidate_id,
        symbol=candidate.symbol,
        timestamp=candidate.scan_timestamp,
        timestamp_status="present" if timestamp_present else "missing",
        status=status,
        lifecycle_bucket=candidate.lifecycle_bucket,
        setup_research_bucket=candidate.setup_research_bucket,
        event_type=event_type,
        description=description,
    )


def _event_type(candidate: ReplayValidationCandidate) -> str:
    if _is_terminal_candidate(candidate):
        return "terminal_outcome"
    if _is_trade_like_candidate(candidate, {}):
        return "trade_like_candidate"
    if _is_negative_example(candidate):
        return "negative_example"
    if candidate.setup_research_bucket != "unknown":
        return candidate.setup_research_bucket
    return candidate.lifecycle_bucket


def _candidate_id(data: Mapping[str, Any], *, source: str, row_index: int) -> str:
    stable_values = [_text(data.get(field_name)) for field_name in ("row_id",) + STABLE_IDENTITY_FIELDS]
    basis = [source]
    if any(_present(value) for value in stable_values):
        basis.extend(stable_values)
    else:
        basis.extend(
            [
                str(row_index),
                _text(data.get("symbol")),
                _text(data.get("timeframe")),
                _text(data.get("status")),
                _text(data.get("scan_timestamp")),
            ]
        )
    digest = hashlib.sha256("\x1f".join(basis).encode("utf-8")).hexdigest()[:16]
    return f"rv-{digest}"


def _is_negative_example(candidate: ReplayValidationCandidate) -> bool:
    return (
        candidate.lifecycle_bucket in NEGATIVE_LIFECYCLE_BUCKETS
        or candidate.setup_research_bucket in NEGATIVE_SETUP_BUCKETS
    )


def _is_trade_like_candidate(candidate: ReplayValidationCandidate, data: Mapping[str, Any]) -> bool:
    if _truthy(data.get("trade_idea_present")) or _truthy(data.get("alert_present")) or _truthy(
        data.get("journal_entry_present")
    ):
        return True
    if _present(candidate.trade_idea_id) or _present(candidate.alert_id):
        return True
    if candidate.setup_research_bucket in {"trade_idea", "alerted", "journaled"}:
        return True
    has_price_context = any(
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
    if has_price_context:
        return True
    return candidate.lifecycle_bucket in TRADE_LIFECYCLE_BUCKETS and not _is_negative_example(candidate)


def _is_terminal_candidate(candidate: ReplayValidationCandidate) -> bool:
    if candidate.lifecycle_bucket in TERMINAL_LIFECYCLE_BUCKETS:
        return True
    if _status_key(candidate.status) in TERMINAL_STATUS_KEYS or _status_key(candidate.normalized_lifecycle_status) in TERMINAL_STATUS_KEYS:
        return True
    return _present(candidate.outcome_status) or _present(candidate.result_r)


def _has_artifact_identity(candidate: ReplayValidationCandidate) -> bool:
    return any(_present(getattr(candidate, field_name)) for field_name in STABLE_IDENTITY_FIELDS)


def _has_entry_context(candidate: ReplayValidationCandidate) -> bool:
    return _present(candidate.entry) or (_present(candidate.entry_low) and _present(candidate.entry_high))


def _candidate_counter(candidates: Sequence[ReplayValidationCandidate], field_name: str) -> dict[str, int]:
    counts = Counter(_text(getattr(candidate, field_name)) for candidate in candidates)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


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


def _first_rejection_reason(row: Mapping[str, Any]) -> str:
    reason = _text(row.get("rejection_reason"))
    if reason != NA:
        return reason
    reasons = _sequence_text(row.get("rejection_reasons"))
    return reasons[0] if reasons else NA


def _sequence_text(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = _text(value)
        return () if text == NA else (text,)
    if isinstance(value, Mapping):
        return ()
    if isinstance(value, Sequence):
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
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
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


def _first_non_na(*values: str) -> str:
    for value in values:
        text = _text(value)
        if text != NA:
            return text
    return NA


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
    "REPLAY_VALIDATION_SCHEMA_VERSION",
    "ReplayValidationCandidate",
    "ReplayValidationEvent",
    "ReplayValidationIssue",
    "ReplayValidationPlan",
    "ReplayValidationResult",
    "ReplayValidationTimeline",
    "build_replay_validation_candidates",
    "build_replay_validation_plan",
    "build_replay_validation_plan_from_files",
    "build_replay_validation_timeline",
    "replay_validation_result_to_dict",
]
