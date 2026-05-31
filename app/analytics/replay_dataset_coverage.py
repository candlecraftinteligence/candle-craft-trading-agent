from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.analytics.lifecycle_replay_audit import normalize_lifecycle_status
from app.analytics.replay_dataset_export import ReplayDatasetRow

NA = "N/A"
REPLAY_DATASET_COVERAGE_SCHEMA_VERSION = "replay_dataset_coverage_v1"

REPLAY_DATASET_COVERAGE_SAFETY_NOTE = (
    "Replay dataset coverage reporting is read-only audit/research only. It does not mutate artifacts, "
    "scanner results, lifecycle states, performance memory, database records, alerts, trade ideas, "
    "strategy behavior, or setup gates; it does not call exchanges, send Telegram messages, execute replay, "
    "create signals, or place trades."
)

IssueSeverity = Literal["warning", "error"]

COVERAGE_DIMENSIONS = (
    "source",
    "artifact_type",
    "row_type",
    "symbol",
    "exchange",
    "market_type",
    "timeframe",
    "strategy_name",
    "strategy_mode",
    "direction",
    "status",
    "normalized_lifecycle_status",
    "lifecycle_bucket",
    "setup_research_bucket",
    "grade",
    "first_failed_gate",
    "rejection_reason",
    "outcome_status",
    "replay_ready",
    "trade_idea_present",
    "alert_present",
    "journal_entry_present",
    "has_result_r",
    "has_missing_data",
    "has_unverified_data",
)

BOOLEAN_DIMENSIONS = {
    "replay_ready",
    "trade_idea_present",
    "alert_present",
    "journal_entry_present",
    "has_result_r",
    "has_missing_data",
    "has_unverified_data",
}
TERMINAL_LIFECYCLE_BUCKETS = {"terminal_tp", "terminal_sl", "closed"}
TERMINAL_OUTCOME_STATUS_KEYS = {
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
SPARSE_OUTPUT_LIMIT = 25
EXAMPLE_LIMIT = 5


@dataclass(frozen=True)
class ReplayDatasetCoverageBucket:
    key: str
    count: int = 0
    coverage_rate: float = 0.0
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayDatasetCoverageDimension:
    dimension_name: str
    total_count: int = 0
    unique_count: int = 0
    coverage_rate: float = 0.0
    buckets: tuple[ReplayDatasetCoverageBucket, ...] = ()
    top_buckets: tuple[ReplayDatasetCoverageBucket, ...] = ()
    sparse_buckets: tuple[ReplayDatasetCoverageBucket, ...] = ()
    sparse_bucket_count: int = 0


@dataclass(frozen=True)
class ReplayDatasetCoverageGap:
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"


@dataclass(frozen=True)
class ReplayDatasetCoverageSummary:
    total_rows: int = 0
    replay_ready_rows: int = 0
    replay_ready_rate: float = 0.0
    symbol_count: int = 0
    source_count: int = 0
    artifact_count: int = 0
    lifecycle_bucket_count: int = 0
    setup_research_bucket_count: int = 0
    no_setup_rows: int = 0
    rejected_rows: int = 0
    trade_idea_rows: int = 0
    alert_rows: int = 0
    journal_entry_rows: int = 0
    terminal_outcome_rows: int = 0
    rows_with_result_r: int = 0
    rows_with_first_failed_gate: int = 0
    rows_with_rejection_reason: int = 0
    sparse_symbol_count: int = 0
    sparse_dimension_bucket_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    is_valid: bool = True


@dataclass(frozen=True)
class ReplayDatasetCoverageResult:
    source: str
    schema_version: str = REPLAY_DATASET_COVERAGE_SCHEMA_VERSION
    is_valid: bool = True
    warning_count: int = 0
    error_count: int = 0
    summary: ReplayDatasetCoverageSummary = field(default_factory=ReplayDatasetCoverageSummary)
    dimensions: tuple[ReplayDatasetCoverageDimension, ...] = ()
    gaps: tuple[ReplayDatasetCoverageGap, ...] = ()
    safety_note: str = REPLAY_DATASET_COVERAGE_SAFETY_NOTE


def analyze_replay_dataset_coverage(
    rows: list[Any],
    source: str = "in_memory",
    top_n: int = 10,
    min_bucket_count: int = 2,
) -> ReplayDatasetCoverageResult:
    gaps: list[ReplayDatasetCoverageGap] = []
    top_n = max(1, int(top_n))
    min_bucket_count = max(1, int(min_bucket_count))

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray, Mapping)):
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="error",
                code="invalid_rows_input",
                message="Replay dataset coverage input must be a sequence of replay rows.",
            )
        )
        return _make_result(source=source, rows=(), dimensions=(), gaps=gaps, min_bucket_count=min_bucket_count)

    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        normalized = _row_to_dict(row)
        if normalized is None:
            gaps.append(
                ReplayDatasetCoverageGap(
                    severity="error",
                    code="invalid_row_input",
                    message="Replay dataset row could not be inspected as a mapping or row object.",
                    path=f"rows[{index}]",
                )
            )
            continue
        normalized_rows.append(_coverage_row(normalized))

    coverage_rows = tuple(normalized_rows)
    dimensions = tuple(_coverage_dimension(name, coverage_rows, top_n, min_bucket_count) for name in COVERAGE_DIMENSIONS)
    summary = _build_summary(coverage_rows, dimensions)
    gaps.extend(_coverage_gaps(summary, dimensions, min_bucket_count))
    return _make_result(
        source=source,
        rows=coverage_rows,
        dimensions=dimensions,
        gaps=gaps,
        min_bucket_count=min_bucket_count,
    )


def analyze_replay_export_coverage_from_files(
    paths: list[Path],
    top_n: int = 10,
    min_bucket_count: int = 2,
) -> ReplayDatasetCoverageResult:
    rows: list[dict[str, Any]] = []
    gaps: list[ReplayDatasetCoverageGap] = []
    sources: list[str] = []

    if not paths:
        gaps.append(
            ReplayDatasetCoverageGap(
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
            gaps.append(
                ReplayDatasetCoverageGap(
                    severity="warning",
                    code="unsupported_csv_input",
                    message="CSV replay dataset input is not supported by this coverage analyzer; use JSONL input.",
                    path=source,
                )
            )
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            gaps.append(
                ReplayDatasetCoverageGap(
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
                gaps.append(
                    ReplayDatasetCoverageGap(
                        severity="error",
                        code="invalid_jsonl",
                        message=f"JSONL line could not be decoded: {exc.msg}",
                        path=f"{source}:{line_number}",
                    )
                )
                continue
            if not isinstance(payload, Mapping):
                gaps.append(
                    ReplayDatasetCoverageGap(
                        severity="error",
                        code="invalid_jsonl_row",
                        message="JSONL replay dataset rows must be JSON objects.",
                        path=f"{source}:{line_number}",
                    )
                )
                continue
            rows.append({str(key): _jsonable(value) for key, value in payload.items()})

    source_label = ", ".join(sources) if sources else "dataset_files"
    result = analyze_replay_dataset_coverage(rows, source=source_label, top_n=top_n, min_bucket_count=min_bucket_count)
    return _with_extra_gaps(result, gaps)


def coverage_result_to_dict(result: ReplayDatasetCoverageResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def classify_lifecycle_bucket(status: Any, normalized_lifecycle_status: Any = NA) -> str:
    explicit = _text(normalized_lifecycle_status)
    fallback = _text(status)
    value = explicit if explicit != NA else fallback
    if value == NA:
        return "unknown"

    normalized = normalize_lifecycle_status(value)
    key = _status_key(normalized)
    raw_key = _status_key(value)

    if key in {"scan_error"} or raw_key in {"scan_error"}:
        return "scan_error"
    if key in {"no_setup", "scanned_no_setup"} or raw_key in {"no_setup", "scanned_no_setup"}:
        return "no_setup"
    if key == "rejected" or raw_key == "rejected" or key.startswith("rejected_") or raw_key.startswith("rejected_"):
        return "rejected"
    if key in {"watch", "watching", "watchlist", "watchlisted", "discovered", "hot_watch"}:
        return "watch"
    if key == "stalking":
        return "stalking"
    if key == "triggered":
        return "triggered"
    if key in {"confirmed", "valid_setup", "trade_idea_created", "idea_created", "alert_created"}:
        return "confirmed"
    if key in {"executing", "managing"}:
        return "executing"
    if key in {"tp_hit", "tp1_hit", "tp2_hit", "tp3_hit", "take_profit_hit", "tp_1_hit", "tp_2_hit", "tp_3_hit"}:
        return "terminal_tp"
    if key in {"sl_hit", "stop_loss_hit", "stopped", "sl"}:
        return "terminal_sl"
    if key in {"invalidated", "expired", "cancelled", "canceled"}:
        return "invalidated"
    if key == "cooldown":
        return "cooldown"
    if key == "closed":
        return "closed"
    return "unknown"


def classify_setup_research_bucket(row: Any) -> str:
    data = _row_to_dict(row)
    if data is None:
        return "unknown"

    if _has_explicit_near_miss(data):
        return "near_miss"
    if _row_has_artifact(data, "journal_entry_present", "journal_entry_id"):
        return "journaled"
    if _row_has_artifact(data, "alert_present", "alert_id"):
        return "alerted"
    if _row_has_artifact(data, "trade_idea_present", "trade_idea_id"):
        return "trade_idea"

    lifecycle_bucket = classify_lifecycle_bucket(data.get("status"), data.get("normalized_lifecycle_status", NA))
    if lifecycle_bucket == "scan_error":
        return "scan_error"
    if lifecycle_bucket == "no_setup":
        return "no_setup"
    if lifecycle_bucket == "rejected":
        return "rejected"
    if _present(data.get("outcome_status")) or _present(data.get("result_r")):
        return "outcome_record"
    if _present(data.get("first_failed_gate")):
        return "gate_failed"

    status_key = _status_key(_text(data.get("status")))
    if status_key in {"gate_failed", "failed_gate", "setup_failed"}:
        return "gate_failed"
    return "unknown"


def _coverage_row(row: Mapping[str, Any]) -> dict[str, str]:
    lifecycle_bucket = classify_lifecycle_bucket(row.get("status"), row.get("normalized_lifecycle_status", NA))
    setup_bucket = classify_setup_research_bucket(row)
    return {
        "source": _text(row.get("source")),
        "artifact_type": _text(row.get("artifact_type")),
        "row_type": _text(row.get("row_type")),
        "symbol": _text(row.get("symbol")).upper() if _text(row.get("symbol")) != NA else NA,
        "exchange": _text(row.get("exchange")),
        "market_type": _text(row.get("market_type")),
        "timeframe": _text(row.get("timeframe")),
        "strategy_name": _text(row.get("strategy_name")),
        "strategy_mode": _text(row.get("strategy_mode")),
        "direction": _text(row.get("direction")).lower() if _text(row.get("direction")) != NA else NA,
        "status": _text(row.get("status")),
        "normalized_lifecycle_status": _text(row.get("normalized_lifecycle_status")),
        "lifecycle_bucket": lifecycle_bucket,
        "setup_research_bucket": setup_bucket,
        "grade": _text(row.get("grade")),
        "first_failed_gate": _text(row.get("first_failed_gate")),
        "rejection_reason": _first_rejection_reason(row),
        "outcome_status": _text(row.get("outcome_status")),
        "replay_ready": _bool_text(_truthy(row.get("replay_ready"))),
        "trade_idea_present": _bool_text(_row_has_artifact(row, "trade_idea_present", "trade_idea_id")),
        "alert_present": _bool_text(_row_has_artifact(row, "alert_present", "alert_id")),
        "journal_entry_present": _bool_text(_row_has_artifact(row, "journal_entry_present", "journal_entry_id")),
        "has_result_r": _bool_text(_present(row.get("result_r"))),
        "has_missing_data": _bool_text(_int_value(row.get("missing_data_count")) > 0 or _truthy(row.get("has_missing_data"))),
        "has_unverified_data": _bool_text(
            _int_value(row.get("unverified_data_count")) > 0 or _truthy(row.get("has_unverified_data"))
        ),
        "row_id": _text(row.get("row_id")),
    }


def _coverage_dimension(
    dimension_name: str,
    rows: tuple[dict[str, str], ...],
    top_n: int,
    min_bucket_count: int,
) -> ReplayDatasetCoverageDimension:
    counts: Counter[str] = Counter()
    examples_by_key: dict[str, list[str]] = defaultdict(list)
    for index, row in enumerate(rows):
        key = _text(row.get(dimension_name))
        counts[key] += 1
        example = _row_example(row, index)
        if len(examples_by_key[key]) < EXAMPLE_LIMIT:
            examples_by_key[key].append(example)

    total_count = len(rows)
    buckets = tuple(
        ReplayDatasetCoverageBucket(
            key=key,
            count=count,
            coverage_rate=_rate(count, total_count),
            examples=tuple(sorted(examples_by_key[key])),
        )
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    missing_keys = _missing_keys_for_dimension(dimension_name)
    covered_count = sum(bucket.count for bucket in buckets if bucket.key not in missing_keys)
    sparse_all = tuple(bucket for bucket in buckets if bucket.count < min_bucket_count and bucket.key != NA)
    return ReplayDatasetCoverageDimension(
        dimension_name=dimension_name,
        total_count=total_count,
        unique_count=len(counts),
        coverage_rate=_rate(covered_count, total_count),
        buckets=buckets,
        top_buckets=buckets[:top_n],
        sparse_buckets=sparse_all[:SPARSE_OUTPUT_LIMIT],
        sparse_bucket_count=len(sparse_all),
    )


def _build_summary(
    rows: tuple[dict[str, str], ...],
    dimensions: tuple[ReplayDatasetCoverageDimension, ...],
) -> ReplayDatasetCoverageSummary:
    total_rows = len(rows)
    dimension_by_name = {dimension.dimension_name: dimension for dimension in dimensions}
    symbols = {row["symbol"] for row in rows if row["symbol"] != NA}
    sources = {row["source"] for row in rows if row["source"] != NA}
    artifacts = {row["artifact_type"] for row in rows if row["artifact_type"] != NA}
    lifecycle_buckets = {row["lifecycle_bucket"] for row in rows}
    setup_buckets = {row["setup_research_bucket"] for row in rows}
    sparse_symbol_count = dimension_by_name.get("symbol", ReplayDatasetCoverageDimension("symbol")).sparse_bucket_count
    sparse_dimension_bucket_count = sum(dimension.sparse_bucket_count for dimension in dimensions)

    return ReplayDatasetCoverageSummary(
        total_rows=total_rows,
        replay_ready_rows=sum(1 for row in rows if row["replay_ready"] == "true"),
        replay_ready_rate=_rate(sum(1 for row in rows if row["replay_ready"] == "true"), total_rows),
        symbol_count=len(symbols),
        source_count=len(sources),
        artifact_count=len(artifacts),
        lifecycle_bucket_count=len(lifecycle_buckets),
        setup_research_bucket_count=len(setup_buckets),
        no_setup_rows=sum(1 for row in rows if row["lifecycle_bucket"] == "no_setup" or row["setup_research_bucket"] == "no_setup"),
        rejected_rows=sum(1 for row in rows if row["lifecycle_bucket"] == "rejected" or row["setup_research_bucket"] == "rejected"),
        trade_idea_rows=sum(1 for row in rows if row["trade_idea_present"] == "true"),
        alert_rows=sum(1 for row in rows if row["alert_present"] == "true"),
        journal_entry_rows=sum(1 for row in rows if row["journal_entry_present"] == "true"),
        terminal_outcome_rows=sum(1 for row in rows if _is_terminal_outcome_row(row)),
        rows_with_result_r=sum(1 for row in rows if row["has_result_r"] == "true"),
        rows_with_first_failed_gate=sum(1 for row in rows if row["first_failed_gate"] != NA),
        rows_with_rejection_reason=sum(1 for row in rows if row["rejection_reason"] != NA),
        sparse_symbol_count=sparse_symbol_count,
        sparse_dimension_bucket_count=sparse_dimension_bucket_count,
    )


def _coverage_gaps(
    summary: ReplayDatasetCoverageSummary,
    dimensions: tuple[ReplayDatasetCoverageDimension, ...],
    min_bucket_count: int,
) -> list[ReplayDatasetCoverageGap]:
    gaps: list[ReplayDatasetCoverageGap] = []
    dimension_by_name = {dimension.dimension_name: dimension for dimension in dimensions}

    if summary.total_rows == 0:
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="no_rows",
                message="Replay dataset contains no rows, so coverage metrics are limited.",
            )
        )
    if summary.symbol_count == 0:
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="no_symbol_coverage",
                message="Replay dataset has no non-N/A symbol coverage.",
                path="dimensions.symbol",
            )
        )

    lifecycle_dimension = dimension_by_name.get("lifecycle_bucket")
    if lifecycle_dimension is not None and _only_bucket(lifecycle_dimension, "unknown"):
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="only_unknown_lifecycle_coverage",
                message="Lifecycle bucket coverage contains only unknown values.",
                path="dimensions.lifecycle_bucket",
            )
        )

    if summary.terminal_outcome_rows == 0:
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="no_terminal_outcome_rows",
                message="Replay dataset has no explicit terminal outcome rows.",
            )
        )
    if summary.rows_with_result_r == 0:
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="no_result_r_coverage",
                message="Replay dataset has no result_r coverage.",
                path="dimensions.has_result_r",
            )
        )
    if summary.trade_idea_rows == 0:
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="no_trade_idea_rows",
                message="Replay dataset has no explicit trade idea rows.",
                path="dimensions.trade_idea_present",
            )
        )

    rejected_or_no_setup_rows = summary.rejected_rows + summary.no_setup_rows
    if (
        summary.total_rows > 0
        and summary.rows_with_first_failed_gate == 0
        and rejected_or_no_setup_rows / summary.total_rows > 0.5
    ):
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="missing_first_failed_gate_for_research_rejections",
                message="Most rows are rejected/no-setup, but no first_failed_gate coverage is present.",
                path="dimensions.first_failed_gate",
            )
        )

    if _too_many_sparse_symbols(summary):
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="many_sparse_symbols",
                message=(
                    f"{summary.sparse_symbol_count} symbol bucket(s) have fewer than {min_bucket_count} row(s); "
                    "symbol coverage may be too thin for per-symbol research."
                ),
                path="dimensions.symbol",
            )
        )

    if summary.total_rows > 0 and summary.replay_ready_rate < 0.8:
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="low_replay_ready_rate",
                message=f"Replay readiness rate is {summary.replay_ready_rate:.2%}, below the 80% research threshold.",
                path="dimensions.replay_ready",
            )
        )

    setup_unknown_rate = _bucket_rate(dimension_by_name.get("setup_research_bucket"), "unknown")
    if setup_unknown_rate > 0.2:
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="high_unknown_setup_research_bucket_rate",
                message=f"Setup research bucket unknown rate is {setup_unknown_rate:.2%}, above the 20% threshold.",
                path="dimensions.setup_research_bucket",
            )
        )

    lifecycle_unknown_rate = _bucket_rate(lifecycle_dimension, "unknown")
    if lifecycle_unknown_rate > 0.2:
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="high_unknown_lifecycle_bucket_rate",
                message=f"Lifecycle bucket unknown rate is {lifecycle_unknown_rate:.2%}, above the 20% threshold.",
                path="dimensions.lifecycle_bucket",
            )
        )

    return gaps


def _make_result(
    *,
    source: str,
    rows: tuple[dict[str, str], ...],
    dimensions: tuple[ReplayDatasetCoverageDimension, ...],
    gaps: Sequence[ReplayDatasetCoverageGap],
    min_bucket_count: int,
) -> ReplayDatasetCoverageResult:
    summary = _build_summary(rows, dimensions)
    warning_count = sum(1 for gap in gaps if gap.severity == "warning")
    error_count = sum(1 for gap in gaps if gap.severity == "error")
    normalized_summary = replace(
        summary,
        warning_count=warning_count,
        error_count=error_count,
        is_valid=error_count == 0,
    )
    return ReplayDatasetCoverageResult(
        source=source,
        is_valid=error_count == 0,
        warning_count=warning_count,
        error_count=error_count,
        summary=normalized_summary,
        dimensions=dimensions,
        gaps=tuple(gaps),
    )


def _with_extra_gaps(
    result: ReplayDatasetCoverageResult,
    extra_gaps: Sequence[ReplayDatasetCoverageGap],
) -> ReplayDatasetCoverageResult:
    if not extra_gaps:
        return result
    gaps = tuple(extra_gaps) + result.gaps
    warning_count = sum(1 for gap in gaps if gap.severity == "warning")
    error_count = sum(1 for gap in gaps if gap.severity == "error")
    summary = replace(result.summary, warning_count=warning_count, error_count=error_count, is_valid=error_count == 0)
    return replace(
        result,
        is_valid=error_count == 0,
        warning_count=warning_count,
        error_count=error_count,
        summary=summary,
        gaps=gaps,
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


def _first_rejection_reason(row: Mapping[str, Any]) -> str:
    reason = _text(row.get("rejection_reason"))
    if reason != NA:
        return reason
    reasons = _sequence_text(row.get("rejection_reasons"))
    return reasons[0] if reasons else NA


def _row_has_artifact(row: Mapping[str, Any], flag_field: str, id_field: str) -> bool:
    return _truthy(row.get(flag_field)) or _present(row.get(id_field))


def _has_explicit_near_miss(row: Mapping[str, Any]) -> bool:
    for field_name in ("status", "normalized_lifecycle_status", "setup_research_bucket", "row_type", "first_failed_gate"):
        if _status_key(_text(row.get(field_name))) == "near_miss":
            return True
    return any(_status_key(value) == "near_miss" for value in _sequence_text(row.get("tags")))


def _is_terminal_outcome_row(row: Mapping[str, str]) -> bool:
    if row.get("lifecycle_bucket") in TERMINAL_LIFECYCLE_BUCKETS:
        return True
    return _status_key(row.get("outcome_status", NA)) in TERMINAL_OUTCOME_STATUS_KEYS


def _missing_keys_for_dimension(dimension_name: str) -> set[str]:
    if dimension_name in BOOLEAN_DIMENSIONS:
        return set()
    if dimension_name in {"lifecycle_bucket", "setup_research_bucket"}:
        return {"unknown"}
    return {NA}


def _only_bucket(dimension: ReplayDatasetCoverageDimension, key: str) -> bool:
    return dimension.total_count > 0 and len(dimension.buckets) == 1 and dimension.buckets[0].key == key


def _bucket_rate(dimension: ReplayDatasetCoverageDimension | None, key: str) -> float:
    if dimension is None or dimension.total_count <= 0:
        return 0.0
    for bucket in dimension.buckets:
        if bucket.key == key:
            return bucket.coverage_rate
    return 0.0


def _too_many_sparse_symbols(summary: ReplayDatasetCoverageSummary) -> bool:
    if summary.symbol_count == 0 or summary.sparse_symbol_count == 0:
        return False
    threshold = max(3, (summary.symbol_count // 2) + 1)
    return summary.sparse_symbol_count >= threshold


def _row_example(row: Mapping[str, str], index: int) -> str:
    row_id = _text(row.get("row_id"))
    if row_id != NA:
        return row_id
    symbol = _text(row.get("symbol"))
    if symbol != NA:
        return f"row[{index}] {symbol}"
    return f"row[{index}]"


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


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


def _status_key(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return ""
    key = text.strip().replace("-", "_").replace(" ", "_").lower()
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


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


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


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
    "REPLAY_DATASET_COVERAGE_SCHEMA_VERSION",
    "ReplayDatasetCoverageBucket",
    "ReplayDatasetCoverageDimension",
    "ReplayDatasetCoverageGap",
    "ReplayDatasetCoverageResult",
    "ReplayDatasetCoverageSummary",
    "analyze_replay_dataset_coverage",
    "analyze_replay_export_coverage_from_files",
    "classify_lifecycle_bucket",
    "classify_setup_research_bucket",
    "coverage_result_to_dict",
]
