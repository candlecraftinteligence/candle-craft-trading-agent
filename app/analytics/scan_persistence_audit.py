from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCAN_PERSISTENCE_AUDIT_SAFETY_NOTE = (
    "Scan persistence audit is read-only. It does not call exchanges, send alerts, execute trades, "
    "or change scanner, lifecycle, strategy, scoring, regime, risk, portfolio, or alert gates."
)

ArtifactType = Literal["scanner_run", "watch_state", "performance_memory", "unknown_json", "invalid_json"]
IssueSeverity = Literal["info", "warning", "error"]

SCANNER_REJECTION_STATUSES = {
    "failed",
    "scan_error",
    "scanned_no_setup",
}
SCANNER_PRICE_FIELDS = {
    "current_price",
    "latest_close",
    "latest_price",
    "mark_price",
    "price",
}
SCANNER_SEQUENCE_FIELDS = {
    "derivatives_missing_data",
    "derivatives_unverified_data",
    "derivatives_warnings",
    "missing_data",
    "rejected_strategy_modes",
    "regime_notes",
    "regime_warnings",
    "strategy_missing_data",
    "strategy_unverified_data",
    "unverified_data",
    "valid_strategy_modes",
    "volume_profile_warnings",
}
WATCH_STATUS_FIELDS = {
    "current_state",
    "display_status",
    "from_state",
    "last_status",
    "lifecycle_status",
    "previous_state",
    "quality_state",
    "readiness_label",
    "status",
    "to_state",
}
TIMESTAMP_FIELDS = {
    "completed_at",
    "first_seen_at",
    "last_seen_at",
    "last_transition_at",
    "run_timestamp",
    "scanned_at",
    "seen_at",
    "started_at",
    "timestamp",
    "updated_at",
}
RUN_ID_FIELDS = {"run_id", "scan_id", "scan_run_id"}
PERFORMANCE_COLLECTION_FIELDS = {
    "counts",
    "memory",
    "outcomes",
    "performance",
    "performance_memory",
    "performance_memory_summary",
    "regime_stats",
    "setup_stats",
    "symbol_health",
    "symbol_health_summary",
    "symbol_stats",
}
PERFORMANCE_R_FIELDS = {
    "average_r",
    "final_r",
    "final_r_multiple",
    "max_drawdown",
    "median_r",
    "r_multiple",
    "result_r",
}
PERFORMANCE_R_SEQUENCE_FIELDS = {"r_multiples"}
PERFORMANCE_OUTCOME_FIELDS = {
    "final_r",
    "final_r_multiple",
    "outcome",
    "outcomes",
    "r_multiple",
    "r_multiples",
    "result_r",
    "setup_stats",
}


class ScanPersistenceAuditIssue(BaseModel):
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"

    model_config = ConfigDict(frozen=True)


class ScanPersistenceAuditSummary(BaseModel):
    artifact_type: ArtifactType
    result_count: int | None = None
    symbol_count: int | None = None
    status_counts: dict[str, int] = Field(default_factory=dict)
    info_count: int = 0
    warning_count: int = 0
    error_count: int = 0

    model_config = ConfigDict(frozen=True)


class ScanPersistenceAuditResult(BaseModel):
    source: str
    is_valid: bool
    artifact_type: ArtifactType
    result_count: int | None = None
    symbol_count: int | None = None
    status_counts: dict[str, int] = Field(default_factory=dict)
    info_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    summary: ScanPersistenceAuditSummary
    issues: tuple[ScanPersistenceAuditIssue, ...] = ()
    inspected_fields: tuple[str, ...] = ()
    safety_note: str = SCAN_PERSISTENCE_AUDIT_SAFETY_NOTE

    model_config = ConfigDict(frozen=True)


def load_json_artifact(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def audit_scan_persistence_file(path: Path) -> ScanPersistenceAuditResult:
    artifact_path = Path(path)
    source = str(artifact_path)
    try:
        data = load_json_artifact(artifact_path)
    except json.JSONDecodeError as exc:
        issue = ScanPersistenceAuditIssue(
            severity="error",
            code="invalid_json",
            message=f"JSON could not be decoded: {exc.msg}",
            path="root",
        )
        return _result(
            source=source,
            artifact_type="invalid_json",
            issues=[issue],
            inspected_fields=(),
        )
    except OSError as exc:
        issue = ScanPersistenceAuditIssue(
            severity="error",
            code="unreadable_json",
            message=f"Artifact could not be read: {exc}",
            path="root",
        )
        return _result(
            source=source,
            artifact_type="invalid_json",
            issues=[issue],
            inspected_fields=(),
        )
    return audit_scan_persistence_artifact(data, source=source)


def audit_scan_persistence_artifact(data: Any, source: str = "in_memory") -> ScanPersistenceAuditResult:
    issues: list[ScanPersistenceAuditIssue] = []
    inspected_fields: list[str] = []
    artifact_type = _classify_artifact(data)
    result_count: int | None = None
    symbol_count: int | None = None
    status_counts: dict[str, int] = {}

    if data == {} or data == []:
        _add_issue(issues, "warning", "empty_artifact", "Artifact is valid JSON but contains no records.")

    if not _is_container(data):
        _add_issue(
            issues,
            "warning",
            "non_container_json",
            "Top-level JSON is not an object or array, so scan persistence structure cannot be inspected.",
        )
        return _result(
            source=source,
            artifact_type=artifact_type,
            result_count=result_count,
            symbol_count=symbol_count,
            status_counts=status_counts,
            issues=issues,
            inspected_fields=inspected_fields,
        )

    if artifact_type == "scanner_run":
        result_count, symbol_count, status_counts = _audit_scanner_run(data, issues, inspected_fields)
    elif artifact_type == "watch_state":
        result_count, symbol_count, status_counts = _audit_watch_state(data, issues, inspected_fields)
    elif artifact_type == "performance_memory":
        result_count, symbol_count, status_counts = _audit_performance_memory(data, issues, inspected_fields)
    else:
        _add_issue(
            issues,
            "warning",
            "unknown_artifact_type",
            "Artifact is JSON, but it does not match known scanner, watch, or performance-memory persistence shapes.",
        )

    _add_issue(
        issues,
        "info",
        "artifact_classified",
        f"Artifact classified as {artifact_type}.",
    )
    return _result(
        source=source,
        artifact_type=artifact_type,
        result_count=result_count,
        symbol_count=symbol_count,
        status_counts=status_counts,
        issues=issues,
        inspected_fields=inspected_fields,
    )


def _audit_scanner_run(
    data: Mapping[str, Any] | list[Any],
    issues: list[ScanPersistenceAuditIssue],
    inspected_fields: list[str],
) -> tuple[int | None, int | None, dict[str, int]]:
    results: list[Any] = []
    if isinstance(data, list):
        results = data
    else:
        if "config" in data:
            _remember(inspected_fields, "config")
            if not isinstance(data.get("config"), Mapping):
                _add_issue(issues, "warning", "config_not_object", "Scanner config should be a JSON object.", "config")
        else:
            _add_issue(issues, "warning", "missing_config", "Scanner run artifact has no top-level config object.")

        if "results" in data:
            _remember(inspected_fields, "results")
            raw_results = data.get("results")
            if isinstance(raw_results, list):
                results = raw_results
            else:
                _add_issue(issues, "warning", "results_not_list", "Scanner results should be a JSON array.", "results")
        else:
            _add_issue(issues, "warning", "missing_results", "Scanner run artifact has no top-level results array.")

    statuses: Counter[str] = Counter()
    symbols: list[str] = []
    for index, raw_result in enumerate(results):
        if not isinstance(raw_result, Mapping):
            _add_issue(
                issues,
                "warning",
                "scanner_result_not_object",
                "Scanner result should be a JSON object.",
                f"results[{index}]",
            )
            continue
        symbol, status = _audit_scanner_symbol_result(raw_result, index, issues, inspected_fields)
        if symbol is not None:
            symbols.append(symbol)
        if status is not None:
            statuses[status] += 1

    result_count = len(results)
    symbol_count = len(set(symbols)) if symbols else 0
    if result_count > 0:
        _audit_run_readiness(data, issues, inspected_fields, "scanner run")
    _add_issue(issues, "info", "scanner_result_count", f"Scanner result count: {result_count}.")
    return result_count, symbol_count, dict(sorted(statuses.items()))


def _audit_scanner_symbol_result(
    raw_result: Mapping[str, Any],
    index: int,
    issues: list[ScanPersistenceAuditIssue],
    inspected_fields: list[str],
) -> tuple[str | None, str | None]:
    path = f"results[{index}]"
    symbol = _optional_text(raw_result.get("symbol"))
    if "symbol" in raw_result:
        _remember(inspected_fields, "results.symbol")
    if symbol is None:
        _add_issue(issues, "warning", "missing_symbol", "Scanner result is missing a symbol.", f"{path}.symbol")

    status = _status_text(raw_result.get("status"))
    if "status" in raw_result:
        _remember(inspected_fields, "results.status")
        if status is None:
            _add_issue(issues, "warning", "status_not_string", "Scanner result status should be a string.", f"{path}.status")
    else:
        _add_issue(issues, "warning", "missing_status", "Scanner result is missing a status.", f"{path}.status")

    status_history = raw_result.get("status_history")
    if "status_history" in raw_result:
        _remember(inspected_fields, "results.status_history")
        if not isinstance(status_history, list):
            _add_issue(
                issues,
                "warning",
                "status_history_not_list",
                "status_history should be a JSON array when present.",
                f"{path}.status_history",
            )

    for field in sorted(SCANNER_PRICE_FIELDS):
        if field not in raw_result:
            continue
        _remember(inspected_fields, f"results.{field}")
        if not _is_json_scalar(raw_result.get(field)):
            _add_issue(
                issues,
                "warning",
                "price_field_not_scalar",
                f"{field} should be a scalar value or N/A when present.",
                f"{path}.{field}",
            )

    if "rejection_reasons" in raw_result:
        _remember(inspected_fields, "results.rejection_reasons")
        if not isinstance(raw_result.get("rejection_reasons"), list):
            _add_issue(
                issues,
                "warning",
                "rejection_reasons_not_list",
                "rejection_reasons should be a JSON array when present.",
                f"{path}.rejection_reasons",
            )
    if "rejection_reason" in raw_result:
        _remember(inspected_fields, "results.rejection_reason")

    if status is not None and _status_requires_rejection_reason(status) and not _has_rejection_reason(raw_result):
        _add_issue(
            issues,
            "warning",
            "missing_rejection_reason",
            "Rejected, no-setup, or scan-error result has no rejection reason.",
            path,
        )

    has_trade_idea = _field_present(raw_result, "trade_idea")
    has_alert = _field_present(raw_result, "alert_result")
    has_journal = _field_present(raw_result, "journal_entry")
    if "trade_idea" in raw_result:
        _remember(inspected_fields, "results.trade_idea")
    if "alert_result" in raw_result:
        _remember(inspected_fields, "results.alert_result")
    if "journal_entry" in raw_result:
        _remember(inspected_fields, "results.journal_entry")

    if has_trade_idea and status == "scanned_no_setup":
        _add_issue(
            issues,
            "warning",
            "trade_idea_on_no_setup",
            "trade_idea is present on a plain scanned_no_setup result.",
            path,
        )
    if has_alert and not has_trade_idea:
        _add_issue(
            issues,
            "warning",
            "alert_without_trade_idea",
            "alert_result is present but trade_idea is missing.",
            path,
        )
    if has_journal and not has_trade_idea:
        _add_issue(
            issues,
            "warning",
            "journal_without_trade_idea",
            "journal_entry is present but trade_idea is missing.",
            path,
        )

    for field in sorted(SCANNER_SEQUENCE_FIELDS):
        if field not in raw_result:
            continue
        _remember(inspected_fields, f"results.{field}")
        if not isinstance(raw_result.get(field), list):
            _add_issue(
                issues,
                "warning",
                "sequence_field_not_list",
                f"{field} should be a JSON array when present.",
                f"{path}.{field}",
            )

    if "strategy_diagnostics" in raw_result:
        _remember(inspected_fields, "results.strategy_diagnostics")
        diagnostics = raw_result.get("strategy_diagnostics")
        if not isinstance(diagnostics, (Mapping, str)):
            _add_issue(
                issues,
                "warning",
                "strategy_diagnostics_wrong_type",
                "strategy_diagnostics should be an object or string when present.",
                f"{path}.strategy_diagnostics",
            )

    return symbol, status


def _audit_watch_state(
    data: Mapping[str, Any] | list[Any],
    issues: list[ScanPersistenceAuditIssue],
    inspected_fields: list[str],
) -> tuple[int | None, int | None, dict[str, int]]:
    records: list[tuple[str, Mapping[str, Any], str | None]] = []
    result_count: int | None = None

    if isinstance(data, list):
        result_count = len(data)
        for index, item in enumerate(data):
            if isinstance(item, Mapping):
                records.append((f"iterations[{index}]", item, None))
            else:
                _add_issue(issues, "warning", "watch_record_not_object", "Watch record should be an object.", f"[{index}]")
    else:
        for field in ("watch", "watch_state", "active_watches"):
            if field in data:
                _remember(inspected_fields, field)
                _extend_watch_records(data.get(field), field, records, issues)

        if "symbols" in data:
            _remember(inspected_fields, "symbols")
            raw_symbols = data.get("symbols")
            if isinstance(raw_symbols, Mapping):
                for symbol, value in raw_symbols.items():
                    if isinstance(value, Mapping):
                        records.append((f"symbols.{symbol}", value, str(symbol)))
                    else:
                        _add_issue(
                            issues,
                            "warning",
                            "watch_symbol_not_object",
                            "Watch symbol state should be an object.",
                            f"symbols.{symbol}",
                        )
            elif isinstance(raw_symbols, list):
                for index, value in enumerate(raw_symbols):
                    if isinstance(value, Mapping):
                        records.append((f"symbols[{index}]", value, None))
                    else:
                        _add_issue(
                            issues,
                            "warning",
                            "watch_symbol_not_object",
                            "Watch symbol state should be an object.",
                            f"symbols[{index}]",
                        )
            else:
                _add_issue(issues, "warning", "watch_symbols_wrong_type", "symbols should be an object or array.", "symbols")

        if "iterations" in data:
            _remember(inspected_fields, "iterations")
            iterations = data.get("iterations")
            if isinstance(iterations, list):
                result_count = len(iterations)
                for index, value in enumerate(iterations):
                    if isinstance(value, Mapping):
                        records.append((f"iterations[{index}]", value, None))
                    else:
                        _add_issue(
                            issues,
                            "warning",
                            "watch_iteration_not_object",
                            "Watch iteration should be an object.",
                            f"iterations[{index}]",
                        )
            else:
                _add_issue(issues, "warning", "iterations_not_list", "iterations should be a JSON array.", "iterations")

        if result_count is None:
            result_count = len(records)
        if not _has_any_key(data, TIMESTAMP_FIELDS):
            _add_issue(
                issues,
                "warning",
                "missing_watch_timestamp",
                "Watch artifact has no top-level timestamp field.",
            )

    statuses: Counter[str] = Counter()
    symbols: list[str] = []
    has_status_field = False
    for path, record, inferred_symbol in records:
        symbol = _optional_text(record.get("symbol")) or _optional_text(inferred_symbol)
        if symbol is not None:
            symbols.append(symbol)
        elif "iteration" in path:
            _add_issue(issues, "warning", "watch_iteration_missing_symbol", "Watch iteration has no symbol field.", path)

        status = _first_status(record)
        if status is None:
            _add_issue(issues, "warning", "watch_record_missing_status", "Watch record has no status-like field.", path)
        else:
            has_status_field = True
            statuses[status] += 1

        for field in WATCH_STATUS_FIELDS:
            if field in record:
                _remember(inspected_fields, f"watch.{field}")
                if not isinstance(record.get(field), str):
                    _add_issue(
                        issues,
                        "warning",
                        "watch_status_not_string",
                        f"{field} should be a string when present.",
                        f"{path}.{field}",
                    )

        if not _has_any_key(record, TIMESTAMP_FIELDS):
            _add_issue(issues, "warning", "watch_record_missing_timestamp", "Watch record has no timestamp.", path)

    if records and not has_status_field:
        _add_issue(
            issues,
            "warning",
            "missing_lifecycle_status_fields",
            "Watch/lifecycle-like records do not expose lifecycle or status fields for replay readiness.",
        )
    if result_count and result_count > 0:
        _audit_run_readiness(data, issues, inspected_fields, "watch artifact")

    symbol_count = len(set(symbols)) if symbols else 0
    _add_issue(issues, "info", "watch_record_count", f"Watch record count: {result_count or 0}.")
    return result_count, symbol_count, dict(sorted(statuses.items()))


def _audit_performance_memory(
    data: Mapping[str, Any] | list[Any],
    issues: list[ScanPersistenceAuditIssue],
    inspected_fields: list[str],
) -> tuple[int | None, int | None, dict[str, int]]:
    if isinstance(data, list):
        result_count = len(data)
        symbol_count = len({_optional_text(item.get("symbol")) for item in data if isinstance(item, Mapping)})
    else:
        for field in sorted(PERFORMANCE_COLLECTION_FIELDS):
            if field not in data:
                continue
            _remember(inspected_fields, field)
            value = data.get(field)
            if not isinstance(value, (Mapping, list)):
                _add_issue(
                    issues,
                    "warning",
                    "performance_collection_wrong_type",
                    f"{field} should be an object or array when present.",
                    field,
                )
        result_count = _performance_record_count(data)
        symbol_count = _performance_symbol_count(data)

    status_counts: Counter[str] = Counter()
    _audit_performance_r_values(data, issues, inspected_fields)
    _collect_outcome_statuses(data, status_counts)

    if not _has_nested_key(data, PERFORMANCE_OUTCOME_FIELDS):
        _add_issue(
            issues,
            "warning",
            "missing_performance_outcomes",
            "Performance-memory-like artifact has no outcome fields for replay validation.",
        )
    if result_count and result_count > 0:
        _audit_run_readiness(data, issues, inspected_fields, "performance memory")

    _add_issue(issues, "info", "performance_record_count", f"Performance record count: {result_count or 0}.")
    return result_count, symbol_count, dict(sorted(status_counts.items()))


def _audit_run_readiness(
    data: Any,
    issues: list[ScanPersistenceAuditIssue],
    inspected_fields: list[str],
    artifact_label: str,
) -> None:
    if not _has_nested_key(data, RUN_ID_FIELDS):
        _add_issue(
            issues,
            "warning",
            "missing_run_id",
            f"{artifact_label} has records but no stable run_id or scan_id.",
        )
    else:
        _remember(inspected_fields, "run_id")
    if not _has_nested_key(data, TIMESTAMP_FIELDS):
        _add_issue(
            issues,
            "warning",
            "missing_run_timestamp",
            f"{artifact_label} has records but no run timestamp.",
        )
    else:
        _remember(inspected_fields, "timestamp")


def _extend_watch_records(
    value: Any,
    path: str,
    records: list[tuple[str, Mapping[str, Any], str | None]],
    issues: list[ScanPersistenceAuditIssue],
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, Mapping):
                records.append((f"{path}.{key}", item, str(key)))
            else:
                _add_issue(issues, "warning", "watch_record_not_object", "Watch record should be an object.", f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, Mapping):
                records.append((f"{path}[{index}]", item, None))
            else:
                _add_issue(issues, "warning", "watch_record_not_object", "Watch record should be an object.", f"{path}[{index}]")
    else:
        _add_issue(issues, "warning", "watch_collection_wrong_type", f"{path} should be an object or array.", path)


def _audit_performance_r_values(
    value: Any,
    issues: list[ScanPersistenceAuditIssue],
    inspected_fields: list[str],
    path: str = "root",
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path != "root" else str(key)
            if key in PERFORMANCE_R_FIELDS:
                _remember(inspected_fields, key)
                if not _is_numeric_like_or_na(item):
                    _add_issue(
                        issues,
                        "warning",
                        "result_r_not_numeric",
                        f"{key} should be numeric-like or N/A when present.",
                        next_path,
                    )
            if key in PERFORMANCE_R_SEQUENCE_FIELDS:
                _remember(inspected_fields, key)
                if not isinstance(item, list):
                    _add_issue(
                        issues,
                        "warning",
                        "result_r_sequence_not_list",
                        f"{key} should be a JSON array when present.",
                        next_path,
                    )
                else:
                    for index, r_value in enumerate(item):
                        if not _is_numeric_like_or_na(r_value):
                            _add_issue(
                                issues,
                                "warning",
                                "result_r_not_numeric",
                                f"{key} entries should be numeric-like or N/A.",
                                f"{next_path}[{index}]",
                            )
            _audit_performance_r_values(item, issues, inspected_fields, next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _audit_performance_r_values(item, issues, inspected_fields, f"{path}[{index}]")


def _collect_outcome_statuses(value: Any, statuses: Counter[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "outcome":
                text = _optional_text(item)
                if text is not None:
                    statuses[text] += 1
            _collect_outcome_statuses(item, statuses)
    elif isinstance(value, list):
        for item in value:
            _collect_outcome_statuses(item, statuses)


def _performance_record_count(data: Mapping[str, Any]) -> int | None:
    for field in ("setup_stats", "outcomes", "performance", "memory"):
        value = data.get(field)
        if isinstance(value, Mapping):
            return len(value)
        if isinstance(value, list):
            return len(value)
    return None


def _performance_symbol_count(data: Mapping[str, Any]) -> int | None:
    for field in ("symbol_stats", "symbol_health", "symbol_health_summary"):
        value = data.get(field)
        if isinstance(value, Mapping):
            return len(value)
        if isinstance(value, list):
            symbols = {_optional_text(item.get("symbol")) for item in value if isinstance(item, Mapping)}
            return len({symbol for symbol in symbols if symbol is not None})
    return None


def _classify_artifact(data: Any) -> ArtifactType:
    if isinstance(data, Mapping):
        keys = set(data)
        if "results" in keys or "config" in keys:
            return "scanner_run"
        if keys & {"active_watches", "iterations", "watch", "watch_state"}:
            return "watch_state"
        if "symbols" in keys and isinstance(data.get("symbols"), (Mapping, list)):
            return "watch_state"
        if keys & PERFORMANCE_COLLECTION_FIELDS:
            return "performance_memory"
        return "unknown_json"
    if isinstance(data, list):
        mappings = [item for item in data if isinstance(item, Mapping)]
        if not mappings:
            return "unknown_json"
        if any({"iteration", "scanned_at", "symbols_watched"} & set(item) for item in mappings):
            return "watch_state"
        if any(PERFORMANCE_OUTCOME_FIELDS & set(item) for item in mappings):
            return "performance_memory"
        if any({"symbol", "status", "trade_idea", "alert_result", "journal_entry"} & set(item) for item in mappings):
            return "scanner_run"
    return "unknown_json"


def _result(
    *,
    source: str,
    artifact_type: ArtifactType,
    issues: list[ScanPersistenceAuditIssue],
    inspected_fields: tuple[str, ...] | list[str],
    result_count: int | None = None,
    symbol_count: int | None = None,
    status_counts: Mapping[str, int] | None = None,
) -> ScanPersistenceAuditResult:
    counts = Counter(issue.severity for issue in issues)
    normalized_status_counts = dict(status_counts or {})
    summary = ScanPersistenceAuditSummary(
        artifact_type=artifact_type,
        result_count=result_count,
        symbol_count=symbol_count,
        status_counts=normalized_status_counts,
        info_count=counts["info"],
        warning_count=counts["warning"],
        error_count=counts["error"],
    )
    return ScanPersistenceAuditResult(
        source=source,
        is_valid=counts["error"] == 0,
        artifact_type=artifact_type,
        result_count=result_count,
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
    issues: list[ScanPersistenceAuditIssue],
    severity: IssueSeverity,
    code: str,
    message: str,
    path: str = "root",
) -> None:
    issues.append(ScanPersistenceAuditIssue(severity=severity, code=code, message=message, path=path))


def _remember(inspected_fields: list[str], field: str) -> None:
    if field not in inspected_fields:
        inspected_fields.append(field)


def _is_container(value: Any) -> bool:
    return isinstance(value, (Mapping, list))


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _status_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _status_requires_rejection_reason(status: str) -> bool:
    return status in SCANNER_REJECTION_STATUSES or status.startswith("rejected_") or "rejected" in status


def _has_rejection_reason(raw_result: Mapping[str, Any]) -> bool:
    if _field_present(raw_result, "rejection_reason"):
        return True
    reasons = raw_result.get("rejection_reasons")
    if isinstance(reasons, list):
        return any(_optional_text(reason) is not None for reason in reasons)
    return False


def _field_present(data: Mapping[str, Any], field: str) -> bool:
    if field not in data:
        return False
    value = data.get(field)
    if value is None:
        return False
    if isinstance(value, str) and _optional_text(value) is None:
        return False
    return True


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return None
    return text


def _first_status(record: Mapping[str, Any]) -> str | None:
    for field in sorted(WATCH_STATUS_FIELDS):
        text = _optional_text(record.get(field))
        if text is not None:
            return text
    return None


def _has_any_key(data: Mapping[str, Any], fields: set[str]) -> bool:
    return any(field in data and _optional_text(data.get(field)) is not None for field in fields)


def _has_nested_key(value: Any, fields: set[str]) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in fields and _optional_text(item) is not None:
                return True
            if _has_nested_key(item, fields):
                return True
    elif isinstance(value, list):
        return any(_has_nested_key(item, fields) for item in value)
    return False


def _is_numeric_like_or_na(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    if isinstance(value, str):
        text = value.strip()
        if text.upper() in {"N/A", "NA"}:
            return True
        if not text:
            return False
        try:
            Decimal(text)
        except (InvalidOperation, ValueError):
            return False
        return True
    return False


__all__ = [
    "SCAN_PERSISTENCE_AUDIT_SAFETY_NOTE",
    "ScanPersistenceAuditIssue",
    "ScanPersistenceAuditResult",
    "ScanPersistenceAuditSummary",
    "audit_scan_persistence_artifact",
    "audit_scan_persistence_file",
    "load_json_artifact",
]
