from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.analytics.replay_dataset_export import ReplayDatasetRow

NA = "N/A"
REPLAY_DATASET_QUALITY_SCHEMA_VERSION = "replay_dataset_quality_v1"

REPLAY_DATASET_QUALITY_SAFETY_NOTE = (
    "Replay dataset quality metrics are read-only research diagnostics. They do not mutate artifacts, "
    "scanner results, lifecycle states, performance memory, database records, alerts, trade ideas, "
    "strategy behavior, or setup gates; they do not call exchanges, send Telegram messages, or execute trades."
)

IssueSeverity = Literal["warning", "error"]

IMPORTANT_FIELDS = (
    "source",
    "artifact_type",
    "row_type",
    "run_id",
    "scan_id",
    "scan_timestamp",
    "symbol",
    "exchange",
    "timeframe",
    "strategy_name",
    "strategy_mode",
    "direction",
    "status",
    "normalized_lifecycle_status",
    "setup_id",
    "trade_idea_id",
    "alert_id",
    "current_price",
    "entry_low",
    "entry_high",
    "stop",
    "invalidation",
    "tp1",
    "tp2",
    "best_rr",
    "confidence_score",
    "grade",
    "first_failed_gate",
    "rejection_reason",
    "result_r",
    "outcome_status",
)
REQUIRED_COMPLETENESS_FIELDS = ("source", "artifact_type", "row_type", "symbol", "status")
CRITICAL_NA_FIELDS = ("symbol", "scan_timestamp", "status", "normalized_lifecycle_status")
STABLE_IDENTIFIER_FIELDS = ("run_id", "scan_id", "setup_id", "trade_idea_id", "alert_id", "journal_entry_id")
IDENTITY_FIELDS = ("row_id",) + STABLE_IDENTIFIER_FIELDS + ("source", "artifact_type", "row_type", "symbol", "scan_timestamp", "status")
TERMINAL_STATUSES = {"TP_HIT", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "STOPPED", "CLOSED"}
RESOLVED_PASSIVE_STATUSES = {
    "CANCELLED",
    "EXPIRED",
    "INVALIDATED",
    "REJECTED",
    "scan_error",
    "scanned_no_setup",
}
UNRESOLVED_STATUSES = {"WATCH", "STALKING", "TRIGGERED", "CONFIRMED", "EXECUTING", "MANAGING"}


@dataclass(frozen=True)
class ReplayDatasetQualityIssue:
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"


@dataclass(frozen=True)
class ReplayDatasetFieldQuality:
    field_name: str
    present_count: int = 0
    missing_count: int = 0
    na_count: int = 0
    completeness_rate: float = 0.0
    unique_count: int | None = None
    missing_row_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReplayDatasetQualitySummary:
    total_rows: int = 0
    replay_ready_rows: int = 0
    replay_not_ready_rows: int = 0
    replay_ready_rate: float = 0.0
    quality_score: float = 0.0
    artifact_type_counts: dict[str, int] = field(default_factory=dict)
    row_type_counts: dict[str, int] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    symbol_count: int = 0
    symbols: tuple[str, ...] = ()
    status_counts: dict[str, int] = field(default_factory=dict)
    normalized_lifecycle_status_counts: dict[str, int] = field(default_factory=dict)
    strategy_name_counts: dict[str, int] = field(default_factory=dict)
    strategy_mode_counts: dict[str, int] = field(default_factory=dict)
    direction_counts: dict[str, int] = field(default_factory=dict)
    grade_counts: dict[str, int] = field(default_factory=dict)
    outcome_status_counts: dict[str, int] = field(default_factory=dict)
    no_setup_rows: int = 0
    trade_idea_rows: int = 0
    alert_rows: int = 0
    journal_entry_rows: int = 0
    rows_with_result_r: int = 0
    rows_with_missing_data: int = 0
    rows_with_unverified_data: int = 0
    total_missing_data_count: int = 0
    total_unverified_data_count: int = 0
    readiness_warning_counts: dict[str, int] = field(default_factory=dict)
    first_failed_gate_counts: dict[str, int] = field(default_factory=dict)
    rejection_reason_counts: dict[str, int] = field(default_factory=dict)
    terminal_outcome_rows: int = 0
    terminal_rows_without_result_or_outcome: int = 0
    unresolved_rows: int = 0
    rows_with_stable_identifier: int = 0
    stable_identifier_coverage_rate: float = 0.0
    duplicate_row_identity_count: int = 0
    duplicate_row_identity_examples: tuple[str, ...] = ()
    field_quality: tuple[ReplayDatasetFieldQuality, ...] = ()
    warning_count: int = 0
    error_count: int = 0
    is_valid: bool = True


@dataclass(frozen=True)
class ReplayDatasetQualityResult:
    source: str
    schema_version: str = REPLAY_DATASET_QUALITY_SCHEMA_VERSION
    is_valid: bool = True
    warning_count: int = 0
    error_count: int = 0
    summary: ReplayDatasetQualitySummary = field(default_factory=ReplayDatasetQualitySummary)
    issues: tuple[ReplayDatasetQualityIssue, ...] = ()
    safety_note: str = REPLAY_DATASET_QUALITY_SAFETY_NOTE


def analyze_replay_rows(rows: list[Any] | tuple[Any, ...], source: str = "in_memory") -> ReplayDatasetQualityResult:
    issues: list[ReplayDatasetQualityIssue] = []
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray, Mapping)):
        issues.append(
            ReplayDatasetQualityIssue(
                severity="error",
                code="invalid_rows_input",
                message="Replay dataset quality input must be a sequence of replay rows.",
            )
        )
        return _make_result(source=source, summary=_build_summary(()), issues=issues)

    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        normalized = _row_to_dict(row)
        if normalized is None:
            issues.append(
                ReplayDatasetQualityIssue(
                    severity="error",
                    code="invalid_row_input",
                    message="Replay dataset row could not be inspected as a mapping or row object.",
                    path=f"rows[{index}]",
                )
            )
            continue
        normalized_rows.append(normalized)

    summary = _build_summary(tuple(normalized_rows))
    issues.extend(_quality_issues(summary))
    return _make_result(source=source, summary=summary, issues=issues)


def analyze_replay_export_result(export_result: Any, source: str = "export_result") -> ReplayDatasetQualityResult:
    rows = getattr(export_result, "rows", ())
    result = analyze_replay_rows(tuple(rows), source=source)

    extra_issues: list[ReplayDatasetQualityIssue] = []
    errors = tuple(getattr(export_result, "errors", ()) or ())
    warnings = tuple(getattr(export_result, "warnings", ()) or ())
    if warnings:
        extra_issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="export_warnings_present",
                message=f"Replay export reported {len(warnings)} warning(s); quality metrics aggregate row readiness warnings.",
            )
        )
    for index, message in enumerate(errors):
        extra_issues.append(
            ReplayDatasetQualityIssue(
                severity="error",
                code="export_error",
                message=str(message),
                path=f"export_result.errors[{index}]",
            )
        )
    return _with_extra_issues(result, extra_issues)


def analyze_replay_dataset_files(paths: list[Path]) -> ReplayDatasetQualityResult:
    rows: list[dict[str, Any]] = []
    issues: list[ReplayDatasetQualityIssue] = []
    sources: list[str] = []

    if not paths:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="no_input_files",
                message="No replay dataset files were provided.",
            )
        )

    for raw_path in paths:
        path = Path(raw_path)
        source = str(path)
        sources.append(source)
        if path.suffix.lower() == ".csv":
            issues.append(
                ReplayDatasetQualityIssue(
                    severity="warning",
                    code="unsupported_csv_input",
                    message="CSV replay dataset input is not supported by this quality analyzer; use JSONL input.",
                    path=source,
                )
            )
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(
                ReplayDatasetQualityIssue(
                    severity="error",
                    code="unreadable_file",
                    message=f"Replay dataset file could not be read: {exc}",
                    path=source,
                )
            )
            continue

        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append(
                    ReplayDatasetQualityIssue(
                        severity="error",
                        code="invalid_jsonl",
                        message=f"JSONL line could not be decoded: {exc.msg}",
                        path=f"{source}:{line_number}",
                    )
                )
                continue
            if not isinstance(payload, Mapping):
                issues.append(
                    ReplayDatasetQualityIssue(
                        severity="error",
                        code="invalid_jsonl_row",
                        message="JSONL replay dataset rows must be JSON objects.",
                        path=f"{source}:{line_number}",
                    )
                )
                continue
            rows.append({str(key): _jsonable(value) for key, value in payload.items()})

    source_label = ", ".join(sources) if sources else "dataset_files"
    result = analyze_replay_rows(rows, source=source_label)
    return _with_extra_issues(result, issues)


def quality_result_to_dict(result: ReplayDatasetQualityResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def _build_summary(rows: tuple[dict[str, Any], ...]) -> ReplayDatasetQualitySummary:
    total_rows = len(rows)
    field_quality = tuple(_field_quality(rows, field_name) for field_name in IMPORTANT_FIELDS)
    field_quality_by_name = {quality.field_name: quality for quality in field_quality}
    replay_ready_rows = sum(1 for row in rows if _truthy(row.get("replay_ready")))
    replay_not_ready_rows = total_rows - replay_ready_rows
    symbols = tuple(sorted({_text(row.get("symbol")) for row in rows if _present(row.get("symbol"))}))
    total_missing_data_count = sum(_int_value(row.get("missing_data_count")) for row in rows)
    total_unverified_data_count = sum(_int_value(row.get("unverified_data_count")) for row in rows)
    readiness_warning_counts = Counter(
        warning
        for row in rows
        for warning in _sequence_text(row.get("replay_readiness_warnings"))
    )
    terminal_rows = [row for row in rows if _is_terminal_row(row)]
    terminal_rows_without_result_or_outcome = sum(
        1 for row in terminal_rows if not (_present(row.get("result_r")) or _present(row.get("outcome_status")))
    )
    duplicate_count, duplicate_examples = _duplicate_identity_metrics(rows)
    rows_with_stable_identifier = sum(1 for row in rows if _has_stable_identifier(row))
    summary = ReplayDatasetQualitySummary(
        total_rows=total_rows,
        replay_ready_rows=replay_ready_rows,
        replay_not_ready_rows=replay_not_ready_rows,
        replay_ready_rate=_rate(replay_ready_rows, total_rows),
        artifact_type_counts=_counter_for_field(rows, "artifact_type", include_na=True),
        row_type_counts=_counter_for_field(rows, "row_type", include_na=True),
        source_counts=_counter_for_field(rows, "source", include_na=True),
        symbol_count=len(symbols),
        symbols=symbols,
        status_counts=_counter_for_field(rows, "status"),
        normalized_lifecycle_status_counts=_counter_for_field(rows, "normalized_lifecycle_status"),
        strategy_name_counts=_counter_for_field(rows, "strategy_name"),
        strategy_mode_counts=_counter_for_field(rows, "strategy_mode"),
        direction_counts=_counter_for_field(rows, "direction"),
        grade_counts=_counter_for_field(rows, "grade"),
        outcome_status_counts=_counter_for_field(rows, "outcome_status"),
        no_setup_rows=sum(1 for row in rows if _is_no_setup_row(row)),
        trade_idea_rows=sum(1 for row in rows if _row_has_artifact(row, "trade_idea_present", "trade_idea_id")),
        alert_rows=sum(1 for row in rows if _row_has_artifact(row, "alert_present", "alert_id")),
        journal_entry_rows=sum(1 for row in rows if _row_has_artifact(row, "journal_entry_present", "journal_entry_id")),
        rows_with_result_r=sum(1 for row in rows if _present(row.get("result_r"))),
        rows_with_missing_data=sum(1 for row in rows if _int_value(row.get("missing_data_count")) > 0),
        rows_with_unverified_data=sum(1 for row in rows if _int_value(row.get("unverified_data_count")) > 0),
        total_missing_data_count=total_missing_data_count,
        total_unverified_data_count=total_unverified_data_count,
        readiness_warning_counts=dict(sorted(readiness_warning_counts.items())),
        first_failed_gate_counts=_counter_for_field(rows, "first_failed_gate"),
        rejection_reason_counts=_counter_for_field(rows, "rejection_reason"),
        terminal_outcome_rows=len(terminal_rows),
        terminal_rows_without_result_or_outcome=terminal_rows_without_result_or_outcome,
        unresolved_rows=sum(1 for row in rows if _is_unresolved_row(row)),
        rows_with_stable_identifier=rows_with_stable_identifier,
        stable_identifier_coverage_rate=_rate(rows_with_stable_identifier, total_rows),
        duplicate_row_identity_count=duplicate_count,
        duplicate_row_identity_examples=duplicate_examples,
        field_quality=field_quality,
    )
    return replace(summary, quality_score=_quality_score(summary, field_quality_by_name, rows))


def _quality_issues(summary: ReplayDatasetQualitySummary) -> list[ReplayDatasetQualityIssue]:
    issues: list[ReplayDatasetQualityIssue] = []
    total_rows = summary.total_rows
    field_quality = {quality.field_name: quality for quality in summary.field_quality}

    if total_rows == 0:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="no_rows",
                message="Replay dataset contains no rows, so quality metrics are limited.",
            )
        )
        return issues

    if summary.replay_ready_rate < 0.8:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="low_replay_ready_rate",
                message=f"Replay readiness rate is {summary.replay_ready_rate:.2%}, below the 80% research-readiness threshold.",
            )
        )

    symbol_missingness = _missing_rate(field_quality, "symbol", total_rows)
    if symbol_missingness > 0.05:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="high_symbol_missingness",
                message=f"Symbol missingness is {symbol_missingness:.2%}, above the 5% threshold.",
            )
        )

    timestamp_missingness = _missing_rate(field_quality, "scan_timestamp", total_rows)
    if timestamp_missingness > 0.2:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="high_timestamp_missingness",
                message=f"Timestamp missingness is {timestamp_missingness:.2%}, above the 20% threshold.",
            )
        )

    identifier_missingness = 1.0 - summary.stable_identifier_coverage_rate
    if identifier_missingness > 0.2:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="high_identifier_missingness",
                message=f"Stable identifier missingness is {identifier_missingness:.2%}, above the 20% threshold.",
            )
        )

    if not summary.status_counts:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="no_status_coverage",
                message="Replay dataset has no non-N/A status coverage.",
            )
        )

    if not summary.normalized_lifecycle_status_counts:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="no_lifecycle_coverage",
                message="Replay dataset has no non-N/A normalized lifecycle status coverage.",
            )
        )

    if summary.duplicate_row_identity_count:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="duplicate_row_identities",
                message=f"Found {summary.duplicate_row_identity_count} duplicate replay row identity occurrence(s).",
            )
        )

    if summary.terminal_rows_without_result_or_outcome:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="terminal_rows_without_result_or_outcome",
                message=(
                    f"{summary.terminal_rows_without_result_or_outcome} terminal row(s) are missing both "
                    "result_r and outcome_status."
                ),
            )
        )

    excessive_fields = tuple(
        field_name
        for field_name in CRITICAL_NA_FIELDS
        if field_name in field_quality and _missing_rate(field_quality, field_name, total_rows) > 0.5
    )
    if excessive_fields:
        issues.append(
            ReplayDatasetQualityIssue(
                severity="warning",
                code="excessive_na_critical_fields",
                message=f"Critical fields have excessive N/A coverage: {', '.join(excessive_fields)}.",
            )
        )

    return issues


def _quality_score(
    summary: ReplayDatasetQualitySummary,
    field_quality: Mapping[str, ReplayDatasetFieldQuality],
    rows: tuple[dict[str, Any], ...],
) -> float:
    if summary.total_rows == 0:
        return 0.0

    readiness_points = 40.0 * summary.replay_ready_rate
    required_rates = [field_quality[field_name].completeness_rate for field_name in REQUIRED_COMPLETENESS_FIELDS]
    required_points = 25.0 * _average(required_rates)
    timestamp_rate = field_quality["scan_timestamp"].completeness_rate
    identifier_rate = summary.stable_identifier_coverage_rate
    timestamp_identifier_points = 15.0 * _average((timestamp_rate, identifier_rate))
    status_rate = max(
        field_quality["status"].completeness_rate,
        field_quality["normalized_lifecycle_status"].completeness_rate,
    )
    lifecycle_rate = field_quality["normalized_lifecycle_status"].completeness_rate
    lifecycle_status_points = 10.0 * _average((status_rate, lifecycle_rate))

    if summary.terminal_outcome_rows:
        terminal_rows = [row for row in rows if _is_terminal_row(row)]
        terminal_covered = sum(
            1 for row in terminal_rows if _present(row.get("result_r")) or _present(row.get("outcome_status"))
        )
        outcome_rate = _rate(terminal_covered, len(terminal_rows))
    else:
        outcome_rate = 1.0
    outcome_points = 10.0 * outcome_rate

    score = readiness_points + required_points + timestamp_identifier_points + lifecycle_status_points + outcome_points
    return round(max(0.0, min(100.0, score)), 2)


def _field_quality(rows: tuple[dict[str, Any], ...], field_name: str) -> ReplayDatasetFieldQuality:
    total_rows = len(rows)
    present_count = 0
    na_count = 0
    complete_count = 0
    unique_values: set[str] = set()
    missing_indices: list[int] = []

    for index, row in enumerate(rows):
        if field_name in row:
            present_count += 1
        value = row.get(field_name, NA)
        if _present(value):
            complete_count += 1
            unique_values.add(_text(value))
        else:
            if field_name in row and _is_na(value):
                na_count += 1
            if len(missing_indices) < 5:
                missing_indices.append(index)

    return ReplayDatasetFieldQuality(
        field_name=field_name,
        present_count=present_count,
        missing_count=total_rows - complete_count,
        na_count=na_count,
        completeness_rate=_rate(complete_count, total_rows),
        unique_count=len(unique_values),
        missing_row_indices=tuple(missing_indices),
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


def _with_extra_issues(
    result: ReplayDatasetQualityResult,
    extra_issues: Sequence[ReplayDatasetQualityIssue],
) -> ReplayDatasetQualityResult:
    if not extra_issues:
        return result
    return _make_result(
        source=result.source,
        summary=result.summary,
        issues=list(extra_issues) + list(result.issues),
    )


def _make_result(
    *,
    source: str,
    summary: ReplayDatasetQualitySummary,
    issues: Sequence[ReplayDatasetQualityIssue],
) -> ReplayDatasetQualityResult:
    warning_count = sum(1 for issue in issues if issue.severity == "warning")
    error_count = sum(1 for issue in issues if issue.severity == "error")
    normalized_summary = replace(
        summary,
        warning_count=warning_count,
        error_count=error_count,
        is_valid=error_count == 0,
    )
    return ReplayDatasetQualityResult(
        source=source,
        is_valid=error_count == 0,
        warning_count=warning_count,
        error_count=error_count,
        summary=normalized_summary,
        issues=tuple(issues),
    )


def _duplicate_identity_metrics(rows: tuple[dict[str, Any], ...]) -> tuple[int, tuple[str, ...]]:
    identities: Counter[str] = Counter()
    for row in rows:
        identity = _row_identity(row)
        if identity is not None:
            identities[identity] += 1
    duplicates = [(identity, count) for identity, count in identities.items() if count > 1]
    duplicate_count = sum(count - 1 for _identity, count in duplicates)
    examples = tuple(f"{identity} ({count} rows)" for identity, count in sorted(duplicates)[:5])
    return duplicate_count, examples


def _row_identity(row: Mapping[str, Any]) -> str | None:
    row_id = _text(row.get("row_id"))
    if row_id != NA:
        return f"row_id={row_id}"

    has_stable_identifier = any(_present(row.get(field_name)) for field_name in STABLE_IDENTIFIER_FIELDS)
    if not has_stable_identifier:
        return None
    parts = []
    for field_name in IDENTITY_FIELDS:
        value = _text(row.get(field_name))
        if value != NA:
            parts.append(f"{field_name}={value}")
    return "|".join(parts) if parts else None


def _counter_for_field(rows: tuple[dict[str, Any], ...], field_name: str, *, include_na: bool = False) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = _text(row.get(field_name))
        if value == NA and not include_na:
            continue
        counts[value] += 1
    return dict(sorted(counts.items()))


def _missing_rate(
    field_quality: Mapping[str, ReplayDatasetFieldQuality],
    field_name: str,
    total_rows: int,
) -> float:
    if total_rows == 0:
        return 0.0
    quality = field_quality[field_name]
    return quality.missing_count / total_rows


def _row_has_artifact(row: Mapping[str, Any], flag_field: str, id_field: str) -> bool:
    return _truthy(row.get(flag_field)) or _present(row.get(id_field))


def _has_stable_identifier(row: Mapping[str, Any]) -> bool:
    return any(_present(row.get(field_name)) for field_name in STABLE_IDENTIFIER_FIELDS)


def _is_no_setup_row(row: Mapping[str, Any]) -> bool:
    values = (_text(row.get("status")), _text(row.get("normalized_lifecycle_status")))
    return any(_status_key(value) in {"no_setup", "scanned_no_setup"} for value in values if value != NA)


def _is_terminal_row(row: Mapping[str, Any]) -> bool:
    values = (_text(row.get("normalized_lifecycle_status")), _text(row.get("status")))
    for value in values:
        if value == NA:
            continue
        if value in TERMINAL_STATUSES or _status_key(value).upper() in TERMINAL_STATUSES:
            return True
    return False


def _is_unresolved_row(row: Mapping[str, Any]) -> bool:
    if _is_terminal_row(row) or _is_no_setup_row(row):
        return False
    status = _text(row.get("normalized_lifecycle_status"))
    if status == NA:
        status = _text(row.get("status"))
    if status == NA:
        return True
    normalized = status if status in UNRESOLVED_STATUSES | RESOLVED_PASSIVE_STATUSES else _status_key(status).upper()
    if normalized in RESOLVED_PASSIVE_STATUSES:
        return False
    return normalized in UNRESOLVED_STATUSES and not (_present(row.get("result_r")) or _present(row.get("outcome_status")))


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


def _int_value(value: Any) -> int:
    if isinstance(value, bool) or _is_na(value):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    return text in {"1", "true", "yes", "y"}


def _present(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return bool(value)
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
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    return str(value).strip()


def _status_key(value: str) -> str:
    key = value.strip().replace("-", "_").replace(" ", "_").lower()
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _average(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


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
    "REPLAY_DATASET_QUALITY_SCHEMA_VERSION",
    "ReplayDatasetFieldQuality",
    "ReplayDatasetQualityIssue",
    "ReplayDatasetQualityResult",
    "ReplayDatasetQualitySummary",
    "analyze_replay_dataset_files",
    "analyze_replay_export_result",
    "analyze_replay_rows",
    "quality_result_to_dict",
]
