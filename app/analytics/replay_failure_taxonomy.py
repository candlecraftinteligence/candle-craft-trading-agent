from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.analytics.replay_dataset_coverage import classify_lifecycle_bucket, classify_setup_research_bucket
from app.analytics.replay_dataset_export import ReplayDatasetRow

NA = "N/A"
REPLAY_FAILURE_TAXONOMY_SCHEMA_VERSION = "replay_failure_taxonomy_v1"

REPLAY_FAILURE_TAXONOMY_SAFETY_NOTE = (
    "Replay failure taxonomy reporting is read-only audit/research only. It groups explicit replay "
    "rejection and failure evidence; it does not mutate artifacts, scanner results, lifecycle states, "
    "performance memory, database records, alerts, trade ideas, strategy behavior, setup gates, or "
    "market data; it does not call exchanges, send Telegram messages, execute replay, create signals, "
    "or place trades."
)

FAILURE_TAXONOMY_DIMENSIONS = (
    "failure_family",
    "first_failed_gate",
    "rejection_reason",
    "failure_reason",
    "status",
    "normalized_lifecycle_status",
    "lifecycle_bucket",
    "setup_research_bucket",
    "symbol",
    "exchange",
    "timeframe",
    "strategy_name",
    "strategy_mode",
    "direction",
    "grade",
    "artifact_type",
    "source",
)

PATTERN_DIMENSIONS = (
    ("failure_family+strategy_mode", ("failure_family", "strategy_mode")),
    ("failure_family+symbol", ("failure_family", "symbol")),
    ("failure_family+timeframe", ("failure_family", "timeframe")),
    ("failure_family+direction", ("failure_family", "direction")),
    ("first_failed_gate+strategy_mode", ("first_failed_gate", "strategy_mode")),
    ("rejection_reason+strategy_mode", ("rejection_reason", "strategy_mode")),
    ("lifecycle_bucket+setup_research_bucket", ("lifecycle_bucket", "setup_research_bucket")),
)

FAILURE_STATUS_KEYS = {
    "cancelled",
    "canceled",
    "invalidated",
    "no_setup",
    "rejected",
    "scan_error",
    "scanned_no_setup",
    "sl",
    "sl_hit",
    "stop_loss_hit",
    "stopped",
}
FAILURE_STATUS_FRAGMENTS = ("failed", "rejected")
FAILURE_SETUP_BUCKETS = {"gate_failed", "no_setup", "rejected", "scan_error"}
FAILURE_LIFECYCLE_BUCKETS = {"invalidated", "no_setup", "rejected", "scan_error", "terminal_sl"}
REJECTED_OR_NO_SETUP_BUCKETS = {"no_setup", "rejected"}
EXAMPLE_LIMIT = 5


@dataclass(frozen=True)
class ReplayFailureTaxonomyBucket:
    key: str
    count: int = 0
    percentage_of_failure_rows: float = 0.0
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayFailureTaxonomyDimension:
    dimension_name: str
    total_count: int = 0
    unique_count: int = 0
    buckets: tuple[ReplayFailureTaxonomyBucket, ...] = ()
    top_buckets: tuple[ReplayFailureTaxonomyBucket, ...] = ()


@dataclass(frozen=True)
class ReplayFailurePattern:
    pattern_name: str
    key: str
    count: int = 0
    percentage_of_failure_rows: float = 0.0
    example_symbols: tuple[str, ...] = ()
    example_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayFailureTaxonomySummary:
    total_rows: int = 0
    failure_rows: int = 0
    non_failure_rows: int = 0
    failure_row_rate: float = 0.0
    unique_failure_families: int = 0
    unique_failure_reasons: int = 0
    rows_with_first_failed_gate: int = 0
    rows_with_rejection_reason: int = 0
    rows_with_replay_readiness_warnings: int = 0
    no_setup_failure_rows: int = 0
    scan_error_rows: int = 0
    invalidated_rows: int = 0
    stopped_or_sl_rows: int = 0
    unknown_failure_rows: int = 0
    top_failure_family: str = NA
    warning_count: int = 0
    error_count: int = 0
    is_valid: bool = True


@dataclass(frozen=True)
class ReplayFailureTaxonomyResult:
    source: str
    schema_version: str = REPLAY_FAILURE_TAXONOMY_SCHEMA_VERSION
    is_valid: bool = True
    warning_count: int = 0
    error_count: int = 0
    summary: ReplayFailureTaxonomySummary = field(default_factory=ReplayFailureTaxonomySummary)
    dimensions: tuple[ReplayFailureTaxonomyDimension, ...] = ()
    patterns: tuple[ReplayFailurePattern, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    safety_note: str = REPLAY_FAILURE_TAXONOMY_SAFETY_NOTE


def analyze_replay_failure_taxonomy(
    rows: list[Any],
    source: str = "in_memory",
    top_n: int = 10,
) -> ReplayFailureTaxonomyResult:
    warnings: list[str] = []
    errors: list[str] = []
    top_n = max(1, int(top_n))

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray, Mapping)):
        errors.append("invalid_rows_input: Replay failure taxonomy input must be a sequence of replay rows.")
        return _make_result(source=source, total_rows=0, failure_rows=(), dimensions=(), patterns=(), warnings=warnings, errors=errors)

    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        normalized = _row_to_dict(row)
        if normalized is None:
            errors.append(
                "invalid_row_input: Replay failure taxonomy row could not be inspected "
                f"as a mapping or row object at rows[{index}]."
            )
            continue
        normalized_rows.append(_taxonomy_base_row(normalized))

    failure_rows = tuple(row for row in normalized_rows if _is_failure_row(row))
    total_rows = len(normalized_rows)
    prepared_failure_rows = tuple(_failure_row(row) for row in failure_rows)
    dimensions = tuple(_taxonomy_dimension(name, prepared_failure_rows, top_n) for name in FAILURE_TAXONOMY_DIMENSIONS)
    patterns = _failure_patterns(prepared_failure_rows, top_n)
    warnings.extend(_taxonomy_warnings(total_rows, prepared_failure_rows))

    return _make_result(
        source=source,
        total_rows=total_rows,
        failure_rows=prepared_failure_rows,
        dimensions=dimensions,
        patterns=patterns,
        warnings=warnings,
        errors=errors,
    )


def analyze_replay_failure_taxonomy_from_files(
    paths: list[Path],
    top_n: int = 10,
) -> ReplayFailureTaxonomyResult:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    sources: list[str] = []

    if not paths:
        warnings.append("no_input_files: No replay dataset files were provided.")

    for raw_path in paths:
        path = Path(raw_path)
        source = str(path)
        sources.append(source)
        if path.suffix.lower() == ".csv":
            warnings.append(
                "unsupported_csv_input: CSV replay dataset input is not supported by this failure taxonomy "
                f"analyzer; use JSONL input. path={source}"
            )
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"unreadable_file: Replay dataset file could not be read: {exc}. path={source}")
            continue

        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"invalid_jsonl: JSONL line could not be decoded: {exc.msg}. path={source}:{line_number}")
                continue
            if not isinstance(payload, Mapping):
                errors.append(f"invalid_jsonl_row: JSONL replay dataset rows must be JSON objects. path={source}:{line_number}")
                continue
            rows.append({str(key): _jsonable(value) for key, value in payload.items()})

    source_label = ", ".join(sources) if sources else "dataset_files"
    result = analyze_replay_failure_taxonomy(rows, source=source_label, top_n=top_n)
    return _with_messages(result, warnings=warnings, errors=errors)


def failure_taxonomy_result_to_dict(result: ReplayFailureTaxonomyResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def classify_failure_family(row: Any) -> str:
    data = _row_to_dict(row)
    if data is None:
        return "unknown_failure"
    normalized = _taxonomy_base_row(data)

    status_key = _status_key(normalized["status"])
    normalized_status_key = _status_key(normalized["normalized_lifecycle_status"])
    lifecycle_bucket = normalized["lifecycle_bucket"]
    setup_bucket = normalized["setup_research_bucket"]
    gate = normalized["first_failed_gate"]
    reasons = _rejection_reasons(normalized)
    warnings = _sequence_text(normalized.get("replay_readiness_warnings"))
    text_values = tuple(value.lower() for value in (gate, *reasons) if value != NA)

    if status_key == "scan_error" or normalized_status_key == "scan_error" or lifecycle_bucket == "scan_error":
        return "scan_error"
    if _contains_any(text_values, ("structure_shift", "bos", "choch")):
        return "missing_structure_shift"
    if _contains_any(text_values, ("confirmation",)):
        return "missing_confirmation"
    if _contains_any(text_values, ("pullback", "ob", "fvg", "fib", "acceptance")):
        return "pullback_failure"
    if _contains_any(text_values, ("rr", "risk_reward", "risk reward", "minimum")):
        return "rr_failure"
    if _contains_any(text_values, ("trust",)):
        return "trust_meter_failure"
    if _contains_any(text_values, ("derivatives", "funding", "oi", "crowding")):
        return "derivatives_conflict"
    if _contains_any(text_values, ("error", "timeout")):
        return "scan_error"
    if _contains_any(text_values, ("unavailable", "malformed")):
        return "data_quality_issue"
    if _contains_any(text_values, ("no_setup", "no setup")) or "no_setup" in {status_key, normalized_status_key}:
        return "no_setup"
    if status_key == "scanned_no_setup" or normalized_status_key == "scanned_no_setup" or setup_bucket == "no_setup":
        return "no_setup"
    if _is_cancelled(status_key, normalized_status_key):
        return "cancelled"
    if _is_stopped_or_sl(status_key, normalized_status_key, lifecycle_bucket):
        return "stopped_or_sl"
    if _is_invalidated(status_key, normalized_status_key, lifecycle_bucket):
        return "invalidated"
    if _is_rejected(status_key, normalized_status_key, lifecycle_bucket, setup_bucket):
        return "rejected"
    if warnings and not (gate != NA or reasons or _status_suggests_failure(status_key, normalized_status_key)):
        return "replay_readiness_gap"
    if warnings and lifecycle_bucket == "unknown" and setup_bucket == "unknown":
        return "replay_readiness_gap"
    return "unknown_failure"


def extract_failure_reasons(row: Any) -> list[str]:
    data = _row_to_dict(row)
    if data is None:
        return []
    normalized = _taxonomy_base_row(data)
    reasons: list[str] = []

    gate = normalized["first_failed_gate"]
    if gate != NA:
        reasons.append(gate)
    reasons.extend(_rejection_reasons(normalized))

    status = normalized["status"]
    normalized_status = normalized["normalized_lifecycle_status"]
    status_key = _status_key(status)
    normalized_status_key = _status_key(normalized_status)
    if _status_suggests_failure(status_key, normalized_status_key):
        reasons.append(f"status={status if status != NA else normalized_status}")

    setup_bucket = normalized["setup_research_bucket"]
    lifecycle_bucket = normalized["lifecycle_bucket"]
    if setup_bucket in FAILURE_SETUP_BUCKETS:
        reasons.append(f"setup_research_bucket={setup_bucket}")
    if lifecycle_bucket in FAILURE_LIFECYCLE_BUCKETS:
        reasons.append(f"lifecycle_bucket={lifecycle_bucket}")

    for warning in _sequence_text(normalized.get("replay_readiness_warnings")):
        reasons.append(f"replay_readiness_warning={warning}")

    return list(_unique_strings(reasons))


def _taxonomy_base_row(row: Mapping[str, Any]) -> dict[str, Any]:
    lifecycle_bucket = _text(row.get("lifecycle_bucket"))
    if lifecycle_bucket == NA:
        lifecycle_bucket = classify_lifecycle_bucket(row.get("status"), row.get("normalized_lifecycle_status", NA))
    setup_bucket = _text(row.get("setup_research_bucket"))
    if setup_bucket == NA:
        setup_bucket = classify_setup_research_bucket(row)
    return {
        "source": _text(row.get("source")),
        "artifact_type": _text(row.get("artifact_type")),
        "row_type": _text(row.get("row_type")),
        "row_id": _text(row.get("row_id")),
        "symbol": _uppercase(_text(row.get("symbol"))),
        "exchange": _text(row.get("exchange")),
        "timeframe": _text(row.get("timeframe")),
        "strategy_name": _text(row.get("strategy_name")),
        "strategy_mode": _text(row.get("strategy_mode")),
        "direction": _lowercase(_text(row.get("direction"))),
        "status": _text(row.get("status")),
        "normalized_lifecycle_status": _text(row.get("normalized_lifecycle_status")),
        "lifecycle_bucket": lifecycle_bucket,
        "setup_research_bucket": setup_bucket,
        "grade": _text(row.get("grade")),
        "first_failed_gate": _text(row.get("first_failed_gate")),
        "rejection_reason": _first_rejection_reason(row),
        "rejection_reasons": _sequence_text(row.get("rejection_reasons")),
        "replay_readiness_warnings": _sequence_text(row.get("replay_readiness_warnings")),
        "trade_idea_present": _truthy(row.get("trade_idea_present")) or _present(row.get("trade_idea_id")),
    }


def _is_failure_row(row: Mapping[str, Any]) -> bool:
    if _text(row.get("first_failed_gate")) != NA:
        return True
    if _rejection_reasons(row):
        return True
    if _sequence_text(row.get("replay_readiness_warnings")):
        return True

    status_key = _status_key(row.get("status"))
    normalized_status_key = _status_key(row.get("normalized_lifecycle_status"))
    if _status_suggests_failure(status_key, normalized_status_key):
        return True

    setup_bucket = _text(row.get("setup_research_bucket"))
    lifecycle_bucket = _text(row.get("lifecycle_bucket"))
    if setup_bucket in FAILURE_SETUP_BUCKETS:
        return True
    if lifecycle_bucket in FAILURE_LIFECYCLE_BUCKETS:
        return True
    return False


def _failure_row(row: Mapping[str, Any]) -> dict[str, Any]:
    family = classify_failure_family(row)
    reasons = tuple(extract_failure_reasons(row))
    failure_reason = reasons[0] if reasons else family
    return {
        "failure_family": family,
        "first_failed_gate": _text(row.get("first_failed_gate")),
        "rejection_reason": _first_rejection_reason(row),
        "failure_reason": failure_reason,
        "failure_reasons": reasons,
        "status": _text(row.get("status")),
        "normalized_lifecycle_status": _text(row.get("normalized_lifecycle_status")),
        "lifecycle_bucket": _text(row.get("lifecycle_bucket")),
        "setup_research_bucket": _text(row.get("setup_research_bucket")),
        "symbol": _text(row.get("symbol")),
        "exchange": _text(row.get("exchange")),
        "timeframe": _text(row.get("timeframe")),
        "strategy_name": _text(row.get("strategy_name")),
        "strategy_mode": _text(row.get("strategy_mode")),
        "direction": _text(row.get("direction")),
        "grade": _text(row.get("grade")),
        "artifact_type": _text(row.get("artifact_type")),
        "source": _text(row.get("source")),
        "row_id": _text(row.get("row_id")),
        "replay_readiness_warnings": _sequence_text(row.get("replay_readiness_warnings")),
    }


def _taxonomy_dimension(
    dimension_name: str,
    rows: tuple[dict[str, Any], ...],
    top_n: int,
) -> ReplayFailureTaxonomyDimension:
    counts: Counter[str] = Counter()
    examples_by_key: dict[str, list[str]] = defaultdict(list)
    for index, row in enumerate(rows):
        values = _dimension_values(row, dimension_name)
        for key in values:
            counts[key] += 1
            example = _row_example(row, index)
            if len(examples_by_key[key]) < EXAMPLE_LIMIT:
                examples_by_key[key].append(example)

    total_count = len(rows)
    buckets = tuple(
        ReplayFailureTaxonomyBucket(
            key=key,
            count=count,
            percentage_of_failure_rows=_rate(count, total_count),
            examples=tuple(examples_by_key[key]),
        )
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    return ReplayFailureTaxonomyDimension(
        dimension_name=dimension_name,
        total_count=total_count,
        unique_count=len(counts),
        buckets=buckets,
        top_buckets=buckets[:top_n],
    )


def _dimension_values(row: Mapping[str, Any], dimension_name: str) -> tuple[str, ...]:
    if dimension_name == "failure_reason":
        reasons = _sequence_text(row.get("failure_reasons"))
        return reasons if reasons else (_text(row.get("failure_reason")),)
    return (_text(row.get(dimension_name)),)


def _failure_patterns(rows: tuple[dict[str, Any], ...], top_n: int) -> tuple[ReplayFailurePattern, ...]:
    patterns: list[ReplayFailurePattern] = []
    for pattern_name, dimensions in PATTERN_DIMENSIONS:
        patterns.extend(_patterns_for_dimensions(pattern_name, dimensions, rows)[:top_n])
    return tuple(patterns)


def _patterns_for_dimensions(
    pattern_name: str,
    dimensions: tuple[str, str],
    rows: tuple[dict[str, Any], ...],
) -> tuple[ReplayFailurePattern, ...]:
    groups: Counter[str] = Counter()
    symbols: dict[str, list[str]] = defaultdict(list)
    reasons: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        key = _pattern_key(row, dimensions)
        groups[key] += 1
        symbol = _text(row.get("symbol"))
        if symbol != NA and symbol not in symbols[key] and len(symbols[key]) < EXAMPLE_LIMIT:
            symbols[key].append(symbol)
        for reason in _sequence_text(row.get("failure_reasons")):
            if reason not in reasons[key] and len(reasons[key]) < EXAMPLE_LIMIT:
                reasons[key].append(reason)

    total_count = len(rows)
    return tuple(
        ReplayFailurePattern(
            pattern_name=pattern_name,
            key=key,
            count=count,
            percentage_of_failure_rows=_rate(count, total_count),
            example_symbols=tuple(symbols[key]),
            example_reasons=tuple(reasons[key]),
        )
        for key, count in sorted(groups.items(), key=lambda item: (-item[1], item[0]))
    )


def _pattern_key(row: Mapping[str, Any], dimensions: tuple[str, str]) -> str:
    return "|".join(f"{dimension}={_text(row.get(dimension))}" for dimension in dimensions)


def _build_summary(
    total_rows: int,
    failure_rows: tuple[dict[str, Any], ...],
    warnings: Sequence[str],
    errors: Sequence[str],
) -> ReplayFailureTaxonomySummary:
    failure_count = len(failure_rows)
    family_counts = Counter(row["failure_family"] for row in failure_rows)
    top_failure_family = NA
    if family_counts:
        top_failure_family = sorted(family_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    failure_reasons = {
        reason
        for row in failure_rows
        for reason in _sequence_text(row.get("failure_reasons"))
    }
    return ReplayFailureTaxonomySummary(
        total_rows=total_rows,
        failure_rows=failure_count,
        non_failure_rows=max(0, total_rows - failure_count),
        failure_row_rate=_rate(failure_count, total_rows),
        unique_failure_families=len(family_counts),
        unique_failure_reasons=len(failure_reasons),
        rows_with_first_failed_gate=sum(1 for row in failure_rows if row["first_failed_gate"] != NA),
        rows_with_rejection_reason=sum(1 for row in failure_rows if row["rejection_reason"] != NA),
        rows_with_replay_readiness_warnings=sum(1 for row in failure_rows if row["replay_readiness_warnings"]),
        no_setup_failure_rows=sum(1 for row in failure_rows if _is_no_setup_failure(row)),
        scan_error_rows=sum(1 for row in failure_rows if _is_scan_error_failure(row)),
        invalidated_rows=sum(1 for row in failure_rows if _is_invalidated_failure(row)),
        stopped_or_sl_rows=sum(1 for row in failure_rows if _is_stopped_or_sl_failure(row)),
        unknown_failure_rows=family_counts["unknown_failure"],
        top_failure_family=top_failure_family,
        warning_count=len(warnings),
        error_count=len(errors),
        is_valid=len(errors) == 0,
    )


def _taxonomy_warnings(total_rows: int, failure_rows: tuple[dict[str, Any], ...]) -> list[str]:
    warnings: list[str] = []
    failure_count = len(failure_rows)
    if total_rows == 0:
        warnings.append("no_rows: Replay dataset contains no rows, so failure taxonomy metrics are limited.")
    if total_rows > 0 and failure_count == 0:
        warnings.append("no_failure_rows: Replay dataset contains no explicit failure or rejection rows.")
        return warnings

    unknown_count = sum(1 for row in failure_rows if row["failure_family"] == "unknown_failure")
    if _rate(unknown_count, failure_count) > 0.3:
        warnings.append(
            "high_unknown_failure_rate: Unknown failure family rate is above 30% of failure rows; "
            "more explicit gate or rejection evidence may be needed."
        )

    rejected_or_no_setup_rows = tuple(row for row in failure_rows if _is_rejected_or_no_setup(row))
    if rejected_or_no_setup_rows and all(row["first_failed_gate"] == NA for row in rejected_or_no_setup_rows):
        warnings.append(
            "no_first_failed_gate_coverage: Rejected/no-setup rows have no first_failed_gate coverage."
        )
    if rejected_or_no_setup_rows and all(row["rejection_reason"] == NA for row in rejected_or_no_setup_rows):
        warnings.append(
            "no_rejection_reason_coverage: Rejected/no-setup rows have no rejection_reason coverage."
        )

    if any(row["symbol"] == NA for row in failure_rows):
        warnings.append("failure_rows_missing_symbol: At least one failure row is missing symbol.")
    if any(row["strategy_mode"] == NA for row in failure_rows):
        warnings.append("failure_rows_missing_strategy_mode: At least one failure row is missing strategy_mode.")
    if any(row["timeframe"] == NA for row in failure_rows):
        warnings.append("failure_rows_missing_timeframe: At least one failure row is missing timeframe.")

    readiness_gap_count = sum(1 for row in failure_rows if row["failure_family"] == "replay_readiness_gap")
    if readiness_gap_count and _rate(readiness_gap_count, failure_count) > 0.3:
        warnings.append(
            "major_replay_readiness_gap_family: Replay readiness gaps are a major failure family in this taxonomy."
        )
    return warnings


def _make_result(
    *,
    source: str,
    total_rows: int,
    failure_rows: tuple[dict[str, Any], ...],
    dimensions: tuple[ReplayFailureTaxonomyDimension, ...],
    patterns: tuple[ReplayFailurePattern, ...],
    warnings: Sequence[str],
    errors: Sequence[str],
) -> ReplayFailureTaxonomyResult:
    summary = _build_summary(total_rows, failure_rows, warnings, errors)
    return ReplayFailureTaxonomyResult(
        source=source,
        is_valid=len(errors) == 0,
        warning_count=len(warnings),
        error_count=len(errors),
        summary=summary,
        dimensions=dimensions,
        patterns=patterns,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def _with_messages(
    result: ReplayFailureTaxonomyResult,
    *,
    warnings: Sequence[str],
    errors: Sequence[str],
) -> ReplayFailureTaxonomyResult:
    if not warnings and not errors:
        return result
    combined_warnings = tuple(warnings) + result.warnings
    combined_errors = tuple(errors) + result.errors
    summary = replace(
        result.summary,
        warning_count=len(combined_warnings),
        error_count=len(combined_errors),
        is_valid=len(combined_errors) == 0,
    )
    return replace(
        result,
        is_valid=len(combined_errors) == 0,
        warning_count=len(combined_warnings),
        error_count=len(combined_errors),
        summary=summary,
        warnings=combined_warnings,
        errors=combined_errors,
    )


def _first_rejection_reason(row: Mapping[str, Any]) -> str:
    reason = _text(row.get("rejection_reason"))
    if reason != NA:
        return reason
    reasons = _sequence_text(row.get("rejection_reasons"))
    return reasons[0] if reasons else NA


def _rejection_reasons(row: Mapping[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    reason = _text(row.get("rejection_reason"))
    if reason != NA:
        reasons.append(reason)
    reasons.extend(_sequence_text(row.get("rejection_reasons")))
    return _unique_strings(reasons)


def _status_suggests_failure(status_key: str, normalized_status_key: str) -> bool:
    keys = {status_key, normalized_status_key}
    if keys & FAILURE_STATUS_KEYS:
        return True
    return any(fragment in key for key in keys for fragment in FAILURE_STATUS_FRAGMENTS)


def _is_rejected(status_key: str, normalized_status_key: str, lifecycle_bucket: str, setup_bucket: str) -> bool:
    keys = {status_key, normalized_status_key}
    return "rejected" in keys or lifecycle_bucket == "rejected" or setup_bucket == "rejected"


def _is_invalidated(status_key: str, normalized_status_key: str, lifecycle_bucket: str) -> bool:
    keys = {status_key, normalized_status_key}
    return bool(keys & {"invalidated", "expired"}) or lifecycle_bucket == "invalidated"


def _is_cancelled(status_key: str, normalized_status_key: str) -> bool:
    return bool({status_key, normalized_status_key} & {"cancelled", "canceled"})


def _is_stopped_or_sl(status_key: str, normalized_status_key: str, lifecycle_bucket: str) -> bool:
    keys = {status_key, normalized_status_key}
    return bool(keys & {"sl", "sl_hit", "stop_loss_hit", "stopped"}) or lifecycle_bucket == "terminal_sl"


def _is_no_setup_failure(row: Mapping[str, Any]) -> bool:
    return (
        row["failure_family"] == "no_setup"
        or row["lifecycle_bucket"] == "no_setup"
        or row["setup_research_bucket"] == "no_setup"
        or _status_key(row["status"]) in {"no_setup", "scanned_no_setup"}
    )


def _is_scan_error_failure(row: Mapping[str, Any]) -> bool:
    return (
        row["failure_family"] == "scan_error"
        or row["lifecycle_bucket"] == "scan_error"
        or row["setup_research_bucket"] == "scan_error"
        or _status_key(row["status"]) == "scan_error"
    )


def _is_invalidated_failure(row: Mapping[str, Any]) -> bool:
    return row["failure_family"] == "invalidated" or row["lifecycle_bucket"] == "invalidated"


def _is_stopped_or_sl_failure(row: Mapping[str, Any]) -> bool:
    return row["failure_family"] == "stopped_or_sl" or row["lifecycle_bucket"] == "terminal_sl"


def _is_rejected_or_no_setup(row: Mapping[str, Any]) -> bool:
    return (
        row["failure_family"] in REJECTED_OR_NO_SETUP_BUCKETS
        or row["lifecycle_bucket"] in REJECTED_OR_NO_SETUP_BUCKETS
        or row["setup_research_bucket"] in REJECTED_OR_NO_SETUP_BUCKETS
        or _status_key(row["status"]) in {"no_setup", "rejected", "scanned_no_setup"}
    )


def _contains_any(values: Sequence[str], needles: Sequence[str]) -> bool:
    return any(needle in value for value in values for needle in needles)


def _row_example(row: Mapping[str, Any], index: int) -> str:
    row_id = _text(row.get("row_id"))
    if row_id != NA:
        return row_id
    symbol = _text(row.get("symbol"))
    if symbol != NA:
        return f"row[{index}] {symbol}"
    return f"row[{index}]"


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


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        text = _text(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


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
    "REPLAY_FAILURE_TAXONOMY_SCHEMA_VERSION",
    "ReplayFailurePattern",
    "ReplayFailureTaxonomyBucket",
    "ReplayFailureTaxonomyDimension",
    "ReplayFailureTaxonomyResult",
    "ReplayFailureTaxonomySummary",
    "analyze_replay_failure_taxonomy",
    "analyze_replay_failure_taxonomy_from_files",
    "classify_failure_family",
    "extract_failure_reasons",
    "failure_taxonomy_result_to_dict",
]
