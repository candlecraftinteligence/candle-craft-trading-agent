from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, Literal

from app.analytics.lifecycle_replay_audit import extract_lifecycle_records, normalize_lifecycle_status

NA = "N/A"
REPLAY_DATASET_SCHEMA_VERSION = "replay_dataset_v1"

ArtifactType = Literal["scanner_run", "watch_state", "performance_memory", "unknown_json", "invalid_json"]

RUN_ID_FIELDS = ("run_id", "scan_run_id")
SCAN_ID_FIELDS = ("scan_id",)
TIMESTAMP_FIELDS = (
    "scan_timestamp",
    "timestamp",
    "run_timestamp",
    "scanned_at",
    "created_at",
    "updated_at",
    "seen_at",
    "first_seen_at",
    "last_seen_at",
    "last_transition_at",
    "completed_at",
    "started_at",
)
STATUS_FIELDS = (
    "status",
    "lifecycle_status",
    "current_state",
    "state",
    "display_status",
    "readiness_label",
    "last_status",
    "to_state",
)
SYMBOL_FIELDS = ("symbol", "ticker", "market")
EXCHANGE_FIELDS = ("exchange", "exchange_name")
MARKET_TYPE_FIELDS = ("market_type", "market")
TIMEFRAME_FIELDS = ("timeframe", "time_frame", "tf")
STRATEGY_NAME_FIELDS = ("strategy_name", "strategy")
STRATEGY_MODE_FIELDS = ("strategy_mode", "setup_mode", "mode")
DIRECTION_FIELDS = ("direction", "side", "bias")
PRICE_FIELDS = ("current_price", "latest_close", "latest_price", "mark_price", "price")
ENTRY_FIELDS = ("entry", "entry_price", "entry_trigger")
STOP_FIELDS = ("stop", "stop_loss", "stop_price")
INVALIDATION_FIELDS = ("invalidation", "invalidation_reason", "cancel_condition")
TP1_FIELDS = ("tp1", "target_1", "take_profit_1")
TP2_FIELDS = ("tp2", "target_2", "take_profit_2")
TP3_FIELDS = ("tp3", "target_3", "take_profit_3")
BEST_RR_FIELDS = ("best_rr", "best_risk_reward_ratio")
RR_TO_TP2_FIELDS = ("rr_to_tp2", "target_rr_to_tp2")
CONFIDENCE_FIELDS = ("confidence_score", "opportunity_score", "score", "total_score")
GRADE_FIELDS = ("grade", "quality_grade")
FIRST_FAILED_GATE_FIELDS = ("first_failed_gate", "failed_gate", "rejection_stage")
RESULT_R_FIELDS = ("result_r", "final_r", "final_r_multiple", "r_multiple")
OUTCOME_FIELDS = ("outcome_status", "outcome", "result")
SETUP_ID_FIELDS = ("setup_id", "setup_fingerprint", "lifecycle_id")
TRADE_IDEA_ID_FIELDS = ("trade_idea_id", "idea_id", "id")
ALERT_ID_FIELDS = ("alert_id", "id")
JOURNAL_ID_FIELDS = ("journal_entry_id", "journal_id", "id")
SECRET_KEY_FRAGMENTS = ("secret", "token", "password", "private_key", "api_key", "api_secret")

WATCH_COLLECTION_FIELDS = (
    "active_watches",
    "events",
    "history",
    "lifecycles",
    "lifecycle_events",
    "lifecycle_records",
    "records",
    "results",
    "setup_lifecycle_events",
    "setup_lifecycle_records",
    "symbols",
    "watch",
    "watch_state",
    "iterations",
)
PERFORMANCE_COLLECTION_FIELDS = (
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
)


@dataclass(frozen=True)
class ReplayDatasetRow:
    schema_version: str = REPLAY_DATASET_SCHEMA_VERSION
    source: str = NA
    artifact_type: str = "unknown_json"
    row_type: str = NA
    row_id: str = NA
    run_id: str = NA
    scan_id: str = NA
    scan_timestamp: str = NA
    symbol: str = NA
    exchange: str = NA
    market_type: str = NA
    timeframe: str = NA
    strategy_name: str = NA
    strategy_mode: str = NA
    direction: str = NA
    status: str = NA
    normalized_lifecycle_status: str = NA
    status_history: tuple[str, ...] = ()
    setup_id: str = NA
    trade_idea_id: str = NA
    alert_id: str = NA
    journal_entry_id: str = NA
    current_price: str = NA
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
    confidence_score: str = NA
    grade: str = NA
    first_failed_gate: str = NA
    rejection_reason: str = NA
    rejection_reasons: tuple[str, ...] = ()
    valid_strategy_modes: tuple[str, ...] = ()
    rejected_strategy_modes: tuple[str, ...] = ()
    missing_data_count: int = 0
    unverified_data_count: int = 0
    trade_idea_present: bool = False
    alert_present: bool = False
    journal_entry_present: bool = False
    result_r: str = NA
    outcome_status: str = NA
    replay_ready: bool = False
    replay_readiness_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayDatasetExportSummary:
    file_count: int = 0
    row_count: int = 0
    replay_ready_count: int = 0
    replay_not_ready_count: int = 0
    artifact_counts: dict[str, int] = field(default_factory=dict)
    warning_count: int = 0
    error_count: int = 0
    is_valid: bool = True
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayDatasetExportResult:
    rows: tuple[ReplayDatasetRow, ...] = ()
    summary: ReplayDatasetExportSummary = field(default_factory=ReplayDatasetExportSummary)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def extract_replay_rows_from_artifact(data: Any, source: str = "in_memory") -> list[ReplayDatasetRow]:
    rows, _warnings, _artifact_type = _extract_rows_with_warnings(data, source=source)
    return rows


def export_replay_dataset_from_files(paths: list[Path]) -> ReplayDatasetExportResult:
    rows: list[ReplayDatasetRow] = []
    export_warnings: list[str] = []
    errors: list[str] = []
    artifact_counts: Counter[str] = Counter()
    sources: list[str] = []

    if not paths:
        export_warnings.append("No replay export input artifacts were found.")

    for raw_path in paths:
        path = Path(raw_path)
        source = str(path)
        sources.append(source)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            artifact_counts["invalid_json"] += 1
            errors.append(f"{source}: invalid_json: {exc.msg}")
            continue
        except OSError as exc:
            artifact_counts["invalid_json"] += 1
            errors.append(f"{source}: unreadable_json: {exc}")
            continue

        extracted, warnings, artifact_type = _extract_rows_with_warnings(data, source=source)
        artifact_counts[artifact_type] += 1
        rows.extend(extracted)
        export_warnings.extend(warnings)

    row_warnings = [
        f"{row.source} {row.row_id}: {warning}"
        for row in rows
        for warning in row.replay_readiness_warnings
    ]
    warnings = tuple(export_warnings + row_warnings)
    summary = ReplayDatasetExportSummary(
        file_count=len(paths),
        row_count=len(rows),
        replay_ready_count=sum(1 for row in rows if row.replay_ready),
        replay_not_ready_count=sum(1 for row in rows if not row.replay_ready),
        artifact_counts=dict(sorted(artifact_counts.items())),
        warning_count=len(warnings),
        error_count=len(errors),
        is_valid=not errors,
        sources=tuple(sources),
    )
    return ReplayDatasetExportResult(rows=tuple(rows), summary=summary, warnings=warnings, errors=tuple(errors))


def rows_to_jsonl(rows: list[ReplayDatasetRow]) -> str:
    if not rows:
        return ""
    lines = [json.dumps(_row_to_dict(row), ensure_ascii=True, separators=(",", ":")) for row in rows]
    return "\n".join(lines) + "\n"


def rows_to_csv(rows: list[ReplayDatasetRow]) -> str:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=_row_field_names(), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_value(value) for key, value in _row_to_dict(row).items()})
    return output.getvalue()


def _extract_rows_with_warnings(data: Any, *, source: str) -> tuple[list[ReplayDatasetRow], list[str], ArtifactType]:
    artifact_type = _classify_artifact(data)
    warnings: list[str] = []

    if artifact_type == "scanner_run":
        rows = _scanner_rows(data, source=source, artifact_type=artifact_type)
    elif artifact_type == "watch_state":
        rows = _watch_rows(data, source=source, artifact_type=artifact_type)
        if not rows:
            warnings.append(f"{source}: watch_state contained no lifecycle/watch-like records.")
    elif artifact_type == "performance_memory":
        rows = _performance_rows(data, source=source, artifact_type=artifact_type)
        if not rows:
            warnings.append(f"{source}: performance_memory contained no exportable performance records.")
    else:
        rows = []
        warnings.append(f"{source}: unknown_json produced zero replay dataset rows.")

    return rows, warnings, artifact_type


def _scanner_rows(data: Any, *, source: str, artifact_type: ArtifactType) -> list[ReplayDatasetRow]:
    root = data if isinstance(data, Mapping) else {}
    config = _mapping_or_empty(root.get("config"))
    raw_results = data if isinstance(data, list) else root.get("results")
    if not isinstance(raw_results, list):
        return []

    rows: list[ReplayDatasetRow] = []
    for index, raw_result in enumerate(raw_results):
        if not isinstance(raw_result, Mapping):
            continue
        rows.append(_scanner_row(raw_result, root=root, config=config, source=source, artifact_type=artifact_type, index=index))
    return rows


def _scanner_row(
    result: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    config: Mapping[str, Any],
    source: str,
    artifact_type: ArtifactType,
    index: int,
) -> ReplayDatasetRow:
    trade_idea = _mapping_or_empty(result.get("trade_idea"))
    alert_result = _mapping_or_empty(_first_present(result, ("alert_result", "alert")))
    journal_entry = _mapping_or_empty(_first_present(result, ("journal_entry", "journal")))
    replay_result = _mapping_or_empty(result.get("replay_result"))
    diagnostics, diagnostic_mode = _representative_diagnostics(result)
    score_result = _mapping_or_empty(result.get("score_result"))
    score_breakdown = _mapping_or_empty(score_result.get("score_breakdown"))
    risk_decision = _mapping_or_empty(_first_present(result, ("risk_decision", "risk")))
    setup_quality = _mapping_or_empty(result.get("setup_quality"))
    entry_zone = _entry_zone(result, diagnostics, trade_idea)
    stop_loss = _mapping_or_empty(_first_present(trade_idea, ("stop_loss",)))

    status = _first_text((result,), STATUS_FIELDS)
    normalized_status = _normalized_status(status)
    raw_status_history = result.get("status_history")
    status_history = _sequence_text(raw_status_history) if isinstance(raw_status_history, list) else ()
    valid_modes = _sequence_text(result.get("valid_strategy_modes"))
    rejected_modes = _sequence_text(result.get("rejected_strategy_modes"))
    strategy_mode = _first_text((result, diagnostics, config), STRATEGY_MODE_FIELDS)
    if strategy_mode == NA:
        strategy_mode = _text_or_na(diagnostic_mode)
    if strategy_mode == NA and len(valid_modes) == 1:
        strategy_mode = valid_modes[0]
    if strategy_mode == NA and len(rejected_modes) == 1:
        strategy_mode = rejected_modes[0]

    take_profits = _take_profit_values(trade_idea, result, diagnostics)
    warnings = _field_shape_warnings(result)
    row = ReplayDatasetRow(
        source=_text_or_na(source),
        artifact_type=artifact_type,
        row_type="scanner_result",
        row_id=_row_id(source, artifact_type, "scanner_result", index),
        run_id=_first_text((result, root, config), RUN_ID_FIELDS),
        scan_id=_first_text((result, root, config), SCAN_ID_FIELDS),
        scan_timestamp=_first_text((result, root, config), TIMESTAMP_FIELDS),
        symbol=_uppercase(_first_text((result,), SYMBOL_FIELDS)),
        exchange=_first_text((result, config, root), EXCHANGE_FIELDS),
        market_type=_first_text((result, config, root), MARKET_TYPE_FIELDS),
        timeframe=_first_text((result, config, root), TIMEFRAME_FIELDS),
        strategy_name=_first_text((result, diagnostics, config, root), STRATEGY_NAME_FIELDS),
        strategy_mode=strategy_mode,
        direction=_lowercase(_first_text((result, trade_idea, diagnostics), DIRECTION_FIELDS)),
        status=status,
        normalized_lifecycle_status=normalized_status,
        status_history=status_history,
        setup_id=_first_text((result, trade_idea, diagnostics), SETUP_ID_FIELDS),
        trade_idea_id=_first_text((result, trade_idea), TRADE_IDEA_ID_FIELDS),
        alert_id=_first_text((result, alert_result), ALERT_ID_FIELDS),
        journal_entry_id=_first_text((result, journal_entry), JOURNAL_ID_FIELDS),
        current_price=_first_text((result, diagnostics), PRICE_FIELDS),
        entry_low=_first_non_na(
            _first_text((result, diagnostics, trade_idea), ("entry_low",)),
            _first_text((entry_zone,), ("low",)),
        ),
        entry_high=_first_non_na(
            _first_text((result, diagnostics, trade_idea), ("entry_high",)),
            _first_text((entry_zone,), ("high",)),
        ),
        entry=_first_non_na(
            _first_text((result, diagnostics, trade_idea), ENTRY_FIELDS),
            _first_text((entry_zone,), ("price",)),
        ),
        stop=_first_non_na(
            _first_text((result, diagnostics, trade_idea, risk_decision), STOP_FIELDS),
            _first_text((stop_loss,), ("price",)),
        ),
        invalidation=_first_text((result, diagnostics, trade_idea), INVALIDATION_FIELDS),
        tp1=_first_non_na(_first_text((result, diagnostics, trade_idea), TP1_FIELDS), _tp_at(take_profits, 0)),
        tp2=_first_non_na(_first_text((result, diagnostics, trade_idea), TP2_FIELDS), _tp_at(take_profits, 1)),
        tp3=_first_non_na(_first_text((result, diagnostics, trade_idea), TP3_FIELDS), _tp_at(take_profits, 2)),
        best_rr=_first_text((result, diagnostics, score_breakdown, trade_idea, risk_decision, setup_quality), BEST_RR_FIELDS),
        rr_to_tp2=_first_text((result, diagnostics, score_breakdown, setup_quality), RR_TO_TP2_FIELDS),
        confidence_score=_first_text((result, trade_idea, score_result, score_breakdown, diagnostics), CONFIDENCE_FIELDS),
        grade=_first_text((result, trade_idea, score_result, score_breakdown, setup_quality), GRADE_FIELDS),
        first_failed_gate=_first_text((result, diagnostics, setup_quality), FIRST_FAILED_GATE_FIELDS),
        rejection_reason=_first_text((result,), ("rejection_reason",)),
        rejection_reasons=_sequence_text(result.get("rejection_reasons")),
        valid_strategy_modes=valid_modes,
        rejected_strategy_modes=rejected_modes,
        missing_data_count=_count_values(result, ("missing_data", "strategy_missing_data", "derivatives_missing_data")),
        unverified_data_count=_count_values(result, ("unverified_data", "strategy_unverified_data", "derivatives_unverified_data")),
        trade_idea_present=_present_value(trade_idea),
        alert_present=_present_value(alert_result),
        journal_entry_present=_present_value(journal_entry),
        result_r=_first_text((result, journal_entry, replay_result), RESULT_R_FIELDS),
        outcome_status=_first_text((journal_entry, replay_result, result), OUTCOME_FIELDS),
        replay_readiness_warnings=tuple(warnings),
    )
    return _finalize_row(row)


def _watch_rows(data: Any, *, source: str, artifact_type: ArtifactType) -> list[ReplayDatasetRow]:
    rows: list[ReplayDatasetRow] = []
    for index, record in enumerate(extract_lifecycle_records(data, source=source)):
        status = _text_or_na(record.status)
        ids = _ids_from_stable(record.stable_id, record.stable_id_field)
        row = ReplayDatasetRow(
            source=_text_or_na(source),
            artifact_type=artifact_type,
            row_type="watch_record",
            row_id=_row_id(source, artifact_type, "watch_record", index),
            run_id=ids["run_id"],
            scan_id=ids["scan_id"],
            scan_timestamp=_text_or_na(record.timestamp),
            symbol=_uppercase(_text_or_na(record.symbol)),
            strategy_mode=_text_or_na(record.mode),
            direction=_lowercase(_text_or_na(record.direction)),
            status=status,
            normalized_lifecycle_status=_normalized_status(status),
            status_history=tuple(record.status_history or ()),
            setup_id=ids["setup_id"],
            trade_idea_id=ids["trade_idea_id"],
            alert_id=ids["alert_id"],
        )
        rows.append(_finalize_row(row))
    return rows


def _performance_rows(data: Any, *, source: str, artifact_type: ArtifactType) -> list[ReplayDatasetRow]:
    records = _performance_record_items(data)
    rows: list[ReplayDatasetRow] = []
    for index, item in enumerate(records):
        path, record, inherited = item
        fingerprint = _mapping_or_empty(record.get("fingerprint"))
        condition_key = _mapping_or_empty(fingerprint.get("condition_key"))
        outcome_status = _first_text((record,), OUTCOME_FIELDS)
        result_r = _first_text((record,), RESULT_R_FIELDS)
        warnings: list[str] = []
        if outcome_status == NA:
            warnings.append("outcome_status missing; performance export did not fabricate an outcome.")
        if result_r == NA:
            warnings.append("result_r missing; performance export did not fabricate profitability.")

        status = _first_non_na(_first_text((record,), STATUS_FIELDS), outcome_status)
        row = ReplayDatasetRow(
            source=_text_or_na(source),
            artifact_type=artifact_type,
            row_type="performance_record",
            row_id=_row_id(source, artifact_type, "performance_record", path or index),
            run_id=_first_text((record,), RUN_ID_FIELDS),
            scan_id=_first_text((record,), SCAN_ID_FIELDS),
            scan_timestamp=_first_text((record,), TIMESTAMP_FIELDS),
            symbol=_uppercase(_first_non_na(_first_text((record,), SYMBOL_FIELDS), inherited.get("symbol", NA))),
            strategy_name=_first_text((record, condition_key, fingerprint), STRATEGY_NAME_FIELDS),
            strategy_mode=_first_text((record, condition_key, fingerprint), STRATEGY_MODE_FIELDS),
            direction=_lowercase(_first_text((record, condition_key, fingerprint), DIRECTION_FIELDS)),
            status=status,
            normalized_lifecycle_status=_normalized_status(status),
            status_history=_sequence_text(record.get("status_history")),
            setup_id=_first_text((record,), SETUP_ID_FIELDS),
            trade_idea_id=_first_text((record,), TRADE_IDEA_ID_FIELDS),
            alert_id=_first_text((record,), ALERT_ID_FIELDS),
            journal_entry_id=_first_text((record,), JOURNAL_ID_FIELDS),
            result_r=result_r,
            outcome_status=outcome_status,
            replay_readiness_warnings=tuple(warnings),
        )
        rows.append(_finalize_row(row))
    return rows


def _performance_record_items(data: Any) -> list[tuple[str, Mapping[str, Any], dict[str, str]]]:
    records: list[tuple[str, Mapping[str, Any], dict[str, str]]] = []
    if isinstance(data, list):
        for index, item in enumerate(data):
            if isinstance(item, Mapping):
                records.append((f"[{index}]", item, {}))
        return records

    if not isinstance(data, Mapping):
        return records

    for field_name in ("outcomes", "performance", "memory", "performance_memory", "setup_stats", "symbol_stats", "regime_stats"):
        value = data.get(field_name)
        if isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(item, Mapping):
                    inherited = {"symbol": str(key)} if field_name == "symbol_stats" else {}
                    records.append((f"{field_name}.{key}", item, inherited))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    records.append((f"{field_name}[{index}]", item, {}))
    return records


def _classify_artifact(data: Any) -> ArtifactType:
    if isinstance(data, Mapping):
        keys = set(data)
        if "results" in keys or "config" in keys:
            return "scanner_run"
        if keys & {"active_watches", "iterations", "watch", "watch_state"}:
            return "watch_state"
        if "symbols" in keys and isinstance(data.get("symbols"), (Mapping, list)):
            return "watch_state"
        if keys & set(PERFORMANCE_COLLECTION_FIELDS):
            return "performance_memory"
        return "unknown_json"

    if isinstance(data, list):
        mappings = [item for item in data if isinstance(item, Mapping)]
        if not mappings:
            return "unknown_json"
        if any({"iteration", "scanned_at", "symbols_watched"} & set(item) for item in mappings):
            return "watch_state"
        if any(set(OUTCOME_FIELDS) & set(item) or set(RESULT_R_FIELDS) & set(item) for item in mappings):
            return "performance_memory"
        if any({"symbol", "status", "trade_idea", "alert_result", "journal_entry"} & set(item) for item in mappings):
            return "scanner_run"
    return "unknown_json"


def _finalize_row(row: ReplayDatasetRow) -> ReplayDatasetRow:
    warnings = list(row.replay_readiness_warnings)
    source_present = row.source != NA
    symbol_present = row.symbol != NA
    status_present = row.status != NA or row.normalized_lifecycle_status != NA
    identifier_present = any(
        value != NA
        for value in (
            row.run_id,
            row.scan_id,
            row.setup_id,
            row.trade_idea_id,
            row.alert_id,
            row.journal_entry_id,
            row.row_id,
        )
    )

    if not source_present:
        warnings.append("source missing.")
    if not symbol_present:
        warnings.append("symbol missing.")
    if not status_present:
        warnings.append("status missing.")
    if not identifier_present:
        warnings.append("stable identifier missing.")
    elif all(
        value == NA
        for value in (row.run_id, row.scan_id, row.setup_id, row.trade_idea_id, row.alert_id, row.journal_entry_id)
    ):
        warnings.append("artifact stable identifier missing; deterministic row_id fallback used.")
    if row.scan_timestamp == NA:
        warnings.append("timestamp missing.")

    replay_ready = source_present and symbol_present and status_present and identifier_present
    return replace(row, replay_ready=replay_ready, replay_readiness_warnings=tuple(_unique_strings(warnings)))


def _row_to_dict(row: ReplayDatasetRow) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for field_info in fields(ReplayDatasetRow):
        value = getattr(row, field_info.name)
        output[field_info.name] = _jsonable(value)
    return output


def _row_field_names() -> list[str]:
    return [field_info.name for field_info in fields(ReplayDatasetRow)]


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


def _csv_value(value: Any) -> str | int:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(_jsonable(value), ensure_ascii=True, separators=(",", ":"))
    if value is None:
        return NA
    return value


def _representative_diagnostics(result: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    diagnostics = _mapping_or_empty(result.get("strategy_diagnostics"))
    valid_modes = _sequence_text(result.get("valid_strategy_modes"))
    rejected_modes = _sequence_text(result.get("rejected_strategy_modes"))
    for mode in (*valid_modes, *rejected_modes):
        value = diagnostics.get(mode)
        if isinstance(value, Mapping):
            return value, mode
    for mode in sorted(str(key) for key in diagnostics):
        value = diagnostics.get(mode)
        if isinstance(value, Mapping):
            return value, mode
    return {}, NA


def _entry_zone(*sources: Mapping[str, Any]) -> Mapping[str, Any]:
    for source in sources:
        value = source.get("entry_zone")
        if isinstance(value, Mapping):
            return value
    return {}


def _take_profit_values(*sources: Mapping[str, Any]) -> tuple[str, ...]:
    for source in sources:
        for key in ("take_profits", "take_profit_targets", "targets"):
            values = source.get(key)
            if not isinstance(values, list):
                continue
            output: list[str] = []
            for item in values:
                if isinstance(item, Mapping):
                    value = _first_text((item,), ("price", "target_price", "target"))
                else:
                    value = _text_or_na(item)
                if value != NA:
                    output.append(value)
            if output:
                return tuple(output)
    return ()


def _tp_at(values: tuple[str, ...], index: int) -> str:
    return values[index] if len(values) > index else NA


def _ids_from_stable(stable_id: str | None, stable_id_field: str | None) -> dict[str, str]:
    ids = {
        "run_id": NA,
        "scan_id": NA,
        "setup_id": NA,
        "trade_idea_id": NA,
        "alert_id": NA,
    }
    value = _text_or_na(stable_id)
    field_name = _text_or_na(stable_id_field)
    if value == NA or field_name == NA:
        return ids
    normalized = field_name.split(".")[-1]
    if normalized in {"run_id", "scan_run_id"}:
        ids["run_id"] = value
    elif normalized == "scan_id":
        ids["scan_id"] = value
    elif normalized in {"setup_id", "setup_fingerprint", "lifecycle_id"}:
        ids["setup_id"] = value
    elif normalized == "trade_idea_id":
        ids["trade_idea_id"] = value
    elif normalized == "alert_id":
        ids["alert_id"] = value
    return ids


def _field_shape_warnings(result: Mapping[str, Any]) -> tuple[str, ...]:
    warnings: list[str] = []
    if "status_history" in result and not isinstance(result.get("status_history"), list):
        warnings.append("status_history was not a JSON list; exported as an empty list.")
    for field_name in ("rejection_reasons", "valid_strategy_modes", "rejected_strategy_modes"):
        if field_name in result and isinstance(result.get(field_name), Mapping):
            warnings.append(f"{field_name} was not a JSON list; exported conservatively.")
    return tuple(warnings)


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_present(source: Mapping[str, Any], fields_to_try: Sequence[str]) -> Any:
    for field_name in fields_to_try:
        if _is_secret_like_key(field_name):
            continue
        if field_name in source and _present_value(source.get(field_name)):
            return source.get(field_name)
    return None


def _first_text(sources: Sequence[Mapping[str, Any]], fields_to_try: Sequence[str]) -> str:
    for source in sources:
        for field_name in fields_to_try:
            if _is_secret_like_key(field_name):
                continue
            value = source.get(field_name)
            if isinstance(value, Mapping):
                continue
            text = _text_or_na(value)
            if text != NA:
                return text
    return NA


def _first_non_na(*values: str) -> str:
    for value in values:
        if value != NA:
            return value
    return NA


def _sequence_text(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = _text_or_na(value)
        return () if text == NA else (text,)
    if isinstance(value, Mapping):
        return ()
    if isinstance(value, Sequence):
        output = []
        for item in value:
            text = _text_or_na(item)
            if text != NA:
                output.append(text)
        return tuple(output)
    return ()


def _count_values(source: Mapping[str, Any], field_names: Sequence[str]) -> int:
    total = 0
    for field_name in field_names:
        value = source.get(field_name)
        if isinstance(value, Mapping):
            total += len(value)
        else:
            total += len(_sequence_text(value))
    return total


def _text_or_na(value: Any) -> str:
    if value is None:
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, Decimal):
        text = str(value)
    else:
        text = str(value).strip()
    if not text:
        return NA
    if text.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return NA
    return text


def _normalized_status(status: str) -> str:
    if status == NA:
        return NA
    return normalize_lifecycle_status(status)


def _uppercase(value: str) -> str:
    return value.upper() if value != NA else NA


def _lowercase(value: str) -> str:
    return value.lower() if value != NA else NA


def _present_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_present_value(item) for key, item in value.items() if not _is_secret_like_key(str(key)))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_present_value(item) for item in value)
    return _text_or_na(value) != NA


def _row_id(source: str, artifact_type: str, row_type: str, index: object) -> str:
    return f"{artifact_type}:{row_type}:{source}:{index}"


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        text = _text_or_na(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


def _is_secret_like_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


__all__ = [
    "REPLAY_DATASET_SCHEMA_VERSION",
    "ReplayDatasetExportResult",
    "ReplayDatasetExportSummary",
    "ReplayDatasetRow",
    "export_replay_dataset_from_files",
    "extract_replay_rows_from_artifact",
    "rows_to_csv",
    "rows_to_jsonl",
]
