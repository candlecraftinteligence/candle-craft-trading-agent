from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.data.dtos import NA
from app.formatters.scanner_display import (
    RankedSymbolDisplay,
    build_symbol_display,
    display_fields,
    rank_scan_results,
    representative_strategy_diagnostics,
)
from app.pipeline.scanner_runner import ScannerRunResult, ScannerSymbolResult
from app.telegram_admin.client import (
    TelegramAdminClient,
    TelegramAdminConfig,
    TelegramAdminDelivery,
    TelegramAdminDeliveryStatus,
    TelegramAdminTransport,
)

AdminDraftType = Literal[
    "valid_setup",
    "near_miss",
    "target_blocked",
    "lifecycle_degraded",
    "scan_health",
]

ADMIN_DRAFT_TYPES: tuple[str, ...] = (
    "valid_setup",
    "near_miss",
    "target_blocked",
    "lifecycle_degraded",
    "scan_health",
)
ADMIN_DRAFT_DELIVERY_STATUSES: tuple[str, ...] = (
    "dry_run",
    "sent_admin",
    "skipped_disabled",
    "skipped_missing_credentials",
    "failed",
)
DEFAULT_ADMIN_DRAFTS_DIR = Path("scan_runs") / "admin_drafts"
logger = logging.getLogger(__name__)


class AdminDraftRecord(BaseModel):
    draft_id: str
    run_id: str
    created_at: str
    draft_type: AdminDraftType
    symbol: str = NA
    message_preview: str
    source_row_summary: dict[str, Any] = Field(default_factory=dict)
    delivery_status: TelegramAdminDeliveryStatus
    telegram_metadata: dict[str, Any] = Field(default_factory=dict)
    error_message: str = NA
    dedupe_key: str

    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class AdminDraftPersistenceResult:
    path: Path
    created: int
    skipped_duplicates: int


@dataclass(frozen=True)
class AdminDraftRoutingResult:
    report: str
    delivery_status: TelegramAdminDeliveryStatus
    delivery_detail: str
    draft_path: Path | None
    drafts_created: int
    drafts_skipped_duplicate: int
    warning: str = NA
    error_message: str = NA


async def route_admin_scan_report(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    manifest_row: Mapping[str, Any] | None = None,
    settings: Any | None = None,
    config: TelegramAdminConfig | None = None,
    drafts_dir: Path = DEFAULT_ADMIN_DRAFTS_DIR,
    client: TelegramAdminClient | None = None,
    transport: TelegramAdminTransport | None = None,
) -> AdminDraftRoutingResult:
    telegram_config = config or TelegramAdminConfig.from_settings(settings)
    admin_client = client or TelegramAdminClient(telegram_config, transport=transport)
    ranked = _ranked(result, ranked_results)
    run_id = _run_id(result, manifest_row)
    draft_path = admin_draft_path_for_run(run_id, drafts_dir=drafts_dir)
    report = format_admin_scan_report(
        result,
        ranked_results=ranked,
        manifest_row=manifest_row,
        draft_artifact_path=draft_path,
    )
    delivery = _duplicate_sent_admin_delivery(
        draft_path,
        run_id=run_id,
        config=telegram_config,
    )
    if delivery is None:
        delivery = await admin_client.send_admin_report(report)
    if delivery.warning != NA:
        logger.warning(delivery.warning)

    telegram_metadata = _telegram_metadata(delivery.telegram_results)
    drafts = build_admin_drafts(
        result,
        ranked_results=ranked,
        manifest_row=manifest_row,
        delivery_status=delivery.status,
        telegram_metadata=telegram_metadata,
        error_message=delivery.error_message,
        report=report,
    )

    try:
        persistence = persist_admin_drafts(drafts, drafts_dir=drafts_dir)
    except OSError as exc:
        return AdminDraftRoutingResult(
            report=report,
            delivery_status=delivery.status,
            delivery_detail=delivery.detail,
            draft_path=None,
            drafts_created=0,
            drafts_skipped_duplicate=0,
            warning="Telegram admin draft persistence failed safely; scanner persistence remains intact.",
            error_message=type(exc).__name__,
        )

    return AdminDraftRoutingResult(
        report=report,
        delivery_status=delivery.status,
        delivery_detail=delivery.detail,
        draft_path=persistence.path,
        drafts_created=persistence.created,
        drafts_skipped_duplicate=persistence.skipped_duplicates,
        warning=delivery.warning,
        error_message=delivery.error_message,
    )


def build_admin_drafts(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    manifest_row: Mapping[str, Any] | None = None,
    delivery_status: TelegramAdminDeliveryStatus,
    telegram_metadata: Mapping[str, Any] | None = None,
    error_message: str = NA,
    report: str | None = None,
    created_at: str | None = None,
) -> tuple[AdminDraftRecord, ...]:
    timestamp = created_at or _now_utc_iso()
    run_id = _run_id(result, manifest_row)
    manifest_timestamp = _manifest_value(manifest_row, "timestamp", timestamp)
    records: list[AdminDraftRecord] = [
        _draft_record(
            run_id=run_id,
            created_at=timestamp,
            draft_type="scan_health",
            symbol=NA,
            summary=_scan_health_summary(result, manifest_row=manifest_row, timestamp=manifest_timestamp),
            message_preview=_preview(report or format_admin_scan_report(result, ranked_results=ranked_results, manifest_row=manifest_row)),
            delivery_status=delivery_status,
            telegram_metadata=telegram_metadata,
            error_message=error_message,
        )
    ]

    for ranked in _ranked(result, ranked_results):
        symbol_result = ranked.symbol_result
        summary = _source_row_summary(symbol_result, run_id=run_id, timestamp=manifest_timestamp)
        for draft_type in _symbol_draft_types(summary):
            records.append(
                _draft_record(
                    run_id=run_id,
                    created_at=timestamp,
                    draft_type=draft_type,
                    symbol=symbol_result.symbol,
                    summary=summary,
                    message_preview=_symbol_preview(draft_type, summary),
                    delivery_status=delivery_status,
                    telegram_metadata=telegram_metadata,
                    error_message=error_message,
                )
            )

    return _dedupe_records(records)


def persist_admin_drafts(
    drafts: Sequence[AdminDraftRecord],
    *,
    drafts_dir: Path = DEFAULT_ADMIN_DRAFTS_DIR,
) -> AdminDraftPersistenceResult:
    run_id = drafts[0].run_id if drafts else "unknown_run"
    path = admin_draft_path_for_run(run_id, drafts_dir=drafts_dir)
    existing_records = _existing_draft_index(path)
    new_records = [
        draft
        for draft in drafts
        if draft.dedupe_key not in existing_records
        or _should_append_delivery_update(existing_records[draft.dedupe_key], draft)
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    if new_records:
        with path.open("a", encoding="utf-8") as handle:
            for draft in new_records:
                handle.write(json.dumps(draft.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    return AdminDraftPersistenceResult(
        path=path,
        created=len(new_records),
        skipped_duplicates=len(drafts) - len(new_records),
    )


def admin_draft_path_for_run(run_id: str, *, drafts_dir: Path = DEFAULT_ADMIN_DRAFTS_DIR) -> Path:
    return drafts_dir / f"{_safe_filename(run_id)}.jsonl"


def format_admin_scan_report(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    manifest_row: Mapping[str, Any] | None = None,
    draft_artifact_path: Path | str | None = None,
    max_rows_per_section: int = 6,
) -> str:
    ranked = _ranked(result, ranked_results)
    summaries = [_source_row_summary(item.symbol_result, run_id=_run_id(result, manifest_row), timestamp=_timestamp(manifest_row)) for item in ranked]
    valid = [summary for summary in summaries if summary["display_status"] == "valid_setup"]
    near = [
        summary
        for summary in summaries
        if summary["display_status"] == "near_miss" and summary["failed_stage"] != "target_integrity"
    ]
    target_blocked = [summary for summary in summaries if _is_target_blocked_summary(summary)]
    lifecycle_degraded = [
        summary for summary in summaries if summary["lifecycle_integrity_status"] == "STALE_OR_DEGRADED"
    ]
    counts = _report_counts(result, ranked, manifest_row=manifest_row)
    bridge = _public_watchlist_bridge_summary(result, manifest_row=manifest_row)

    lines = [
        "Candle Craft Admin Scan Report",
        "",
        f"Run: {_run_id(result, manifest_row)}",
        f"Universe: {_universe_label(result, manifest_row)}",
        f"Regime: {_market_regime(result, manifest_row)} / confidence {_regime_confidence(result, manifest_row)}",
        f"Symbols scanned: {counts['symbols_scanned']}",
        f"Valid setups: {counts['valid_setups']}",
        f"Near misses: {counts['near_misses']}",
        f"Rejected: {counts['rejected']}",
        f"Target blocked: {len(target_blocked)}",
        f"Lifecycle degraded: {len(lifecycle_degraded)}",
        f"Alerts created: {counts['alerts_created']}",
        f"Trade ideas: {counts['trade_ideas']}",
        f"Journals: {counts['journals']}",
        "public_watchlist_bridge:",
        f"- near_miss_seen: {bridge['near_miss_seen']}",
        f"- near_miss_plan_complete: {bridge['near_miss_plan_complete']}",
        f"- public_watchlist_trade_ideas_created: {bridge['public_watchlist_trade_ideas_created']}",
        f"- public_watchlist_alerts_created: {bridge['public_watchlist_alerts_created']}",
        f"- public_watchlist_sent: {bridge['public_watchlist_sent']}",
        f"- public_watchlist_blocked: {bridge['public_watchlist_blocked']}",
        f"- blocked_before_trade_idea_by_reason: {_bridge_reason_text(bridge['blocked_before_trade_idea_by_reason'])}",
        "admin_drafts_skipped_disabled is separate from public WATCHLIST delivery.",
        f"Runtime: {_runtime_seconds(result, manifest_row)}s",
        f"Draft artifact: {_display(draft_artifact_path)}",
        "",
        "Valid Setups",
        *_valid_setup_lines(valid, max_rows=max_rows_per_section),
        "",
        "Near Misses",
        *_near_miss_lines(near, max_rows=max_rows_per_section),
        "",
        "Target Blocked",
        *_target_blocked_lines(target_blocked, max_rows=max_rows_per_section),
        "",
        "Lifecycle Degraded",
        *_lifecycle_degraded_lines(lifecycle_degraded, max_rows=max_rows_per_section),
        "",
        "No valid setup = no trade.",
        "Admin-only. No public/VIP send.",
        "Admin review only.",
        "No execution behavior enabled.",
    ]
    return "\n".join(lines)


def format_status_report(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    manifest_row: Mapping[str, Any] | None = None,
) -> str:
    return format_admin_scan_report(result, ranked_results=ranked_results, manifest_row=manifest_row)


def format_lastscan_report(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    manifest_row: Mapping[str, Any] | None = None,
) -> str:
    return format_admin_scan_report(result, ranked_results=ranked_results, manifest_row=manifest_row)


def format_blocked_report(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    manifest_row: Mapping[str, Any] | None = None,
) -> str:
    summaries = [
        _source_row_summary(item.symbol_result, run_id=_run_id(result, manifest_row), timestamp=_timestamp(manifest_row))
        for item in _ranked(result, ranked_results)
    ]
    target_blocked = [summary for summary in summaries if _is_target_blocked_summary(summary)]
    return "\n".join(("Target Blocked", *_target_blocked_lines(target_blocked, max_rows=20)))


def format_near_report(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    manifest_row: Mapping[str, Any] | None = None,
) -> str:
    summaries = [
        _source_row_summary(item.symbol_result, run_id=_run_id(result, manifest_row), timestamp=_timestamp(manifest_row))
        for item in _ranked(result, ranked_results)
    ]
    near = [
        summary
        for summary in summaries
        if summary["display_status"] == "near_miss" and summary["failed_stage"] != "target_integrity"
    ]
    return "\n".join(("Near Misses", *_near_miss_lines(near, max_rows=20)))


def _draft_record(
    *,
    run_id: str,
    created_at: str,
    draft_type: AdminDraftType,
    symbol: str,
    summary: Mapping[str, Any],
    message_preview: str,
    delivery_status: TelegramAdminDeliveryStatus,
    error_message: str,
    telegram_metadata: Mapping[str, Any] | None = None,
) -> AdminDraftRecord:
    dedupe_key = _dedupe_key(
        run_id=run_id,
        symbol=symbol,
        draft_type=draft_type,
        failed_stage=_display(summary.get("failed_stage")),
        setup_identifier=_setup_identifier(summary),
    )
    return AdminDraftRecord(
        draft_id=f"draft_{sha256(dedupe_key.encode('utf-8')).hexdigest()[:16]}",
        run_id=run_id,
        created_at=created_at,
        draft_type=draft_type,
        symbol=symbol,
        message_preview=message_preview,
        source_row_summary=dict(summary),
        delivery_status=delivery_status,
        telegram_metadata=dict(telegram_metadata or {}),
        error_message=_display(error_message),
        dedupe_key=dedupe_key,
    )


def _source_row_summary(symbol_result: ScannerSymbolResult, *, run_id: str, timestamp: str) -> dict[str, Any]:
    display = build_symbol_display(symbol_result)
    fields = display_fields(symbol_result)
    diagnostics = representative_strategy_diagnostics(symbol_result)
    target_intelligence = symbol_result.target_intelligence
    target_blocked = display.failed_stage == "target_integrity" or display.failed_gate == "target_integrity"
    if target_blocked:
        target_failure_type = (
            _display(getattr(target_intelligence, "target_failure_type", NA))
            if target_intelligence is not None
            else _display(diagnostics.get("target_failure_type"))
        )
        target_reason = _first_non_na(
            diagnostics.get("target_integrity_reason"),
            getattr(target_intelligence, "rr_compression_reason", NA) if target_intelligence is not None else NA,
            getattr(target_intelligence, "next_target_condition", NA) if target_intelligence is not None else NA,
        )
    else:
        target_failure_type = NA
        target_reason = NA

    return {
        "run_id": run_id,
        "timestamp": timestamp,
        "symbol": symbol_result.symbol,
        "direction": _side(symbol_result, diagnostics),
        "side": _side(symbol_result, diagnostics),
        "grade": _grade(symbol_result, diagnostics),
        "score": _score(symbol_result, diagnostics),
        "display_status": display.display_status,
        "failed_stage": display.failed_stage,
        "failed_gate": display.failed_gate,
        "short_reason": display.short_reason,
        "next_trigger_needed": display.next_trigger_needed,
        "lifecycle_current_state": fields.get("lifecycle_current_state", NA),
        "lifecycle_integrity_status": fields.get("lifecycle_integrity_status", NA),
        "lifecycle_integrity_warning": fields.get("lifecycle_integrity_warning", NA),
        "target_integrity_failure_type": target_failure_type,
        "target_integrity_reason": target_reason,
        "admin_action_suggestion": _admin_action_suggestion(display.display_status, display.failed_stage, fields),
    }


def _scan_health_summary(
    result: ScannerRunResult,
    *,
    manifest_row: Mapping[str, Any] | None,
    timestamp: str,
) -> dict[str, Any]:
    counts = _report_counts(result, _ranked(result, None), manifest_row=manifest_row)
    bridge = _public_watchlist_bridge_summary(result, manifest_row=manifest_row)
    return {
        "run_id": _run_id(result, manifest_row),
        "timestamp": timestamp,
        "symbol": NA,
        "direction": NA,
        "side": NA,
        "grade": NA,
        "score": NA,
        "display_status": "scan_health",
        "failed_stage": NA,
        "failed_gate": NA,
        "short_reason": "Scan completed; review summary before any manual action.",
        "next_trigger_needed": NA,
        "lifecycle_current_state": NA,
        "lifecycle_integrity_status": NA,
        "target_integrity_failure_type": NA,
        "target_integrity_reason": NA,
        "admin_action_suggestion": "Review scan health. No valid setup = no trade.",
        "symbols_scanned": counts["symbols_scanned"],
        "valid_setups": counts["valid_setups"],
        "near_misses": counts["near_misses"],
        "rejected": counts["rejected"],
        "alerts_created": counts["alerts_created"],
        "trade_ideas": counts["trade_ideas"],
        "journals": counts["journals"],
        "public_watchlist_bridge": bridge,
        "admin_drafts_skipped_disabled_separate": True,
    }


def _symbol_draft_types(summary: Mapping[str, Any]) -> tuple[AdminDraftType, ...]:
    draft_types: list[AdminDraftType] = []
    if summary.get("display_status") == "valid_setup":
        draft_types.append("valid_setup")
    if summary.get("display_status") == "near_miss" and summary.get("failed_stage") != "target_integrity":
        draft_types.append("near_miss")
    if _is_target_blocked_summary(summary):
        draft_types.append("target_blocked")
    if summary.get("lifecycle_integrity_status") == "STALE_OR_DEGRADED":
        draft_types.append("lifecycle_degraded")
    return tuple(draft_types)


def _symbol_preview(draft_type: AdminDraftType, summary: Mapping[str, Any]) -> str:
    return (
        f"{draft_type}: {_display(summary.get('symbol'))} | side {_display(summary.get('side'))} | "
        f"grade {_display(summary.get('grade'))} | score {_display(summary.get('score'))} | "
        f"{_display(summary.get('short_reason'))} | next {_display(summary.get('next_trigger_needed'))}"
    )


def _preview(text: str, max_length: int = 700) -> str:
    cleaned = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(cleaned) <= max_length:
        return cleaned
    return f"{cleaned[: max_length - 3].rstrip()}..."


def _valid_setup_lines(summaries: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    if not summaries:
        return ["- None"]
    return _limited_lines(
        [
            (
                f"- {_display(item.get('symbol'))} | {_display(item.get('side'))} | "
                f"{_display(item.get('grade'))} | {_display(item.get('score'))} | "
                f"{_display(item.get('short_reason'))} | {_display(item.get('next_trigger_needed'))}"
            )
            for item in summaries
        ],
        max_rows=max_rows,
    )


def _near_miss_lines(summaries: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    if not summaries:
        return ["- None"]
    return _limited_lines(
        [
            (
                f"- {_display(item.get('symbol'))} | {_display(item.get('failed_stage'))} | "
                f"{_display(item.get('short_reason'))} | {_display(item.get('next_trigger_needed'))}"
            )
            for item in summaries
        ],
        max_rows=max_rows,
    )


def _target_blocked_lines(summaries: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    if not summaries:
        return ["- None"]
    return _limited_lines(
        [
            (
                f"- {_display(item.get('symbol'))} | {_display(item.get('target_integrity_failure_type'))} | "
                f"{_display(item.get('target_integrity_reason'))} | {_display(item.get('next_trigger_needed'))}"
            )
            for item in summaries
        ],
        max_rows=max_rows,
    )


def _lifecycle_degraded_lines(summaries: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    if not summaries:
        return ["- None"]
    return _limited_lines(
        [
            (
                f"- {_display(item.get('symbol'))} | {_display(item.get('lifecycle_current_state'))} | "
                f"{_display(item.get('failed_stage'))} | {_display(item.get('lifecycle_integrity_warning'))}"
            )
            for item in summaries
        ],
        max_rows=max_rows,
    )


def _limited_lines(lines: list[str], *, max_rows: int) -> list[str]:
    if len(lines) <= max_rows:
        return lines
    omitted = len(lines) - max_rows
    return [*lines[:max_rows], f"- {omitted} more omitted"]


def _report_counts(
    result: ScannerRunResult,
    ranked: Sequence[RankedSymbolDisplay],
    *,
    manifest_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    bucket_counts = Counter(item.display.display_bucket for item in ranked)
    return {
        "symbols_scanned": _manifest_value(manifest_row, "symbols_scanned", result.scanned_symbols),
        "valid_setups": _manifest_value(manifest_row, "valid_setup_count", bucket_counts.get("valid", 0)),
        "near_misses": _manifest_value(manifest_row, "near_miss_count", bucket_counts.get("near_miss", 0)),
        "rejected": _manifest_value(manifest_row, "rejected_count", bucket_counts.get("no_setup", 0)),
        "alerts_created": _manifest_value(manifest_row, "alerts_created", result.dry_run_alerts_created),
        "trade_ideas": _manifest_value(manifest_row, "trade_ideas_created", result.trade_ideas_created),
        "journals": _manifest_value(manifest_row, "journal_entries_created", result.journal_entries_created),
    }


def _public_watchlist_bridge_summary(
    result: ScannerRunResult,
    *,
    manifest_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source = None
    if manifest_row is not None:
        maybe_source = manifest_row.get("public_watchlist_bridge")
        if isinstance(maybe_source, Mapping):
            source = maybe_source
    if source is None:
        maybe_source = result.scanner_process_summary.get("public_watchlist_bridge")
        if isinstance(maybe_source, Mapping):
            source = maybe_source
    source = source or {}
    blocked = source.get("blocked_before_trade_idea_by_reason") if isinstance(source, Mapping) else {}
    return {
        "near_miss_seen": int(source.get("near_miss_seen", 0)),
        "near_miss_plan_complete": int(source.get("near_miss_plan_complete", 0)),
        "public_watchlist_trade_ideas_created": int(source.get("public_watchlist_trade_ideas_created", 0)),
        "public_watchlist_alerts_created": int(source.get("public_watchlist_alerts_created", 0)),
        "public_watchlist_sent": int(source.get("public_watchlist_sent", 0)),
        "public_watchlist_blocked": int(source.get("public_watchlist_blocked", 0)),
        "blocked_before_trade_idea_by_reason": dict(blocked) if isinstance(blocked, Mapping) else {},
    }


def _bridge_reason_text(reasons: Mapping[str, Any]) -> str:
    if not reasons:
        return "{}"
    return ", ".join(f"{key}={value}" for key, value in sorted(reasons.items()))


def _dedupe_records(records: Sequence[AdminDraftRecord]) -> tuple[AdminDraftRecord, ...]:
    seen: set[str] = set()
    output: list[AdminDraftRecord] = []
    for record in records:
        if record.dedupe_key in seen:
            continue
        seen.add(record.dedupe_key)
        output.append(record)
    return tuple(output)


def _existing_draft_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            key = _display(payload.get("dedupe_key"))
            if key != NA:
                records[key] = dict(payload)
    return records


def _should_append_delivery_update(existing: Mapping[str, Any], draft: AdminDraftRecord) -> bool:
    existing_status = _display(existing.get("delivery_status"))
    if existing_status == draft.delivery_status:
        return False
    return draft.delivery_status in {"sent_admin", "failed"}


def _duplicate_sent_admin_delivery(
    draft_path: Path,
    *,
    run_id: str,
    config: TelegramAdminConfig,
) -> TelegramAdminDelivery | None:
    if not _live_duplicate_guard_enabled(config, run_id):
        return None
    if not draft_path.exists():
        return None
    for line in draft_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        if _display(payload.get("run_id")) != run_id:
            continue
        if _display(payload.get("delivery_status")) != "sent_admin":
            continue
        metadata = payload.get("telegram_metadata")
        telegram_results = _telegram_results_from_metadata(metadata if isinstance(metadata, Mapping) else {})
        return TelegramAdminDelivery(
            status="sent_admin",
            detail="Telegram admin report already sent for this run_id; duplicate live send skipped.",
            telegram_results=telegram_results,
        )
    return None


def _live_duplicate_guard_enabled(config: TelegramAdminConfig, run_id: str) -> bool:
    return config.admin_report_enabled and not config.dry_run and config.has_admin_credentials and run_id != NA


def _telegram_metadata(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for result in results:
        message_metadata = {
            key: value
            for key in ("message_id", "chat_id", "sent_at")
            if (value := result.get(key)) not in (None, "", NA)
        }
        if message_metadata:
            messages.append(message_metadata)

    if not messages:
        return {}

    metadata: dict[str, Any] = {"messages": messages}
    if len(messages) == 1:
        metadata.update(messages[0])
    return metadata


def _telegram_results_from_metadata(metadata: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    messages = metadata.get("messages")
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        results = tuple(dict(item) for item in messages if isinstance(item, Mapping))
        if results:
            return results
    result = {
        key: value
        for key in ("message_id", "chat_id", "sent_at")
        if (value := metadata.get(key)) not in (None, "", NA)
    }
    return (result,) if result else ()


def _dedupe_key(
    *,
    run_id: str,
    symbol: str,
    draft_type: str,
    failed_stage: str,
    setup_identifier: str,
) -> str:
    return "|".join(
        (
            _display(run_id).lower(),
            _display(symbol).lower(),
            _display(draft_type).lower(),
            _display(failed_stage).lower(),
            _display(setup_identifier).lower(),
        )
    )


def _setup_identifier(summary: Mapping[str, Any]) -> str:
    return _first_non_na(summary.get("failed_gate"), summary.get("side"), summary.get("display_status"))


def _ranked(
    result: ScannerRunResult,
    ranked_results: Sequence[RankedSymbolDisplay] | None,
) -> tuple[RankedSymbolDisplay, ...]:
    if ranked_results is not None:
        return tuple(ranked_results)
    return tuple(rank_scan_results(result.results))


def _run_id(result: ScannerRunResult, manifest_row: Mapping[str, Any] | None) -> str:
    manifest_run_id = _manifest_value(manifest_row, "run_id", NA)
    if manifest_run_id != NA:
        return str(manifest_run_id)
    metadata = result.resume_metadata if isinstance(result.resume_metadata, Mapping) else {}
    for key in ("scan_run_id", "run_id", "storage_run_id"):
        value = _display(metadata.get(key))
        if value != NA:
            return value
    return NA


def _timestamp(manifest_row: Mapping[str, Any] | None) -> str:
    return _manifest_value(manifest_row, "timestamp", _now_utc_iso())


def _universe_label(result: ScannerRunResult, manifest_row: Mapping[str, Any] | None) -> str:
    value = _manifest_value(manifest_row, "universe_label", NA)
    if value != NA:
        return str(value)
    universe = result.resume_metadata.get("universe") if isinstance(result.resume_metadata, Mapping) else None
    if isinstance(universe, Mapping):
        return _first_non_na(universe.get("label"), universe.get("mode"))
    return NA


def _market_regime(result: ScannerRunResult, manifest_row: Mapping[str, Any] | None) -> str:
    value = _manifest_value(manifest_row, "market_regime", NA)
    if value != NA:
        return str(value)
    return _display(getattr(result.market_regime.state, "value", result.market_regime.state))


def _regime_confidence(result: ScannerRunResult, manifest_row: Mapping[str, Any] | None) -> str:
    value = _manifest_value(manifest_row, "regime_confidence", NA)
    if value != NA:
        return str(value)
    return _display(result.market_regime.confidence_score)


def _runtime_seconds(result: ScannerRunResult, manifest_row: Mapping[str, Any] | None) -> str:
    value = _manifest_value(manifest_row, "runtime_seconds", NA)
    if value != NA:
        return str(value)
    return _display(result.runtime_stats.total_runtime_seconds)


def _side(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    trade_idea = symbol_result.trade_idea
    value = _display(getattr(trade_idea, "direction", NA)) if trade_idea is not None else NA
    if value in {"long", "short"}:
        return value
    for key in ("direction", "side", "bias"):
        value = _display(diagnostics.get(key))
        if value in {"long", "short"}:
            return value
    return NA


def _grade(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    trade_idea = symbol_result.trade_idea
    if trade_idea is not None:
        value = _display(getattr(trade_idea, "grade", NA))
        if value != NA:
            return value
    quality = symbol_result.setup_quality
    if getattr(quality, "is_evaluated", False):
        value = _display(getattr(quality.quality_grade, "value", quality.quality_grade))
        if value != NA:
            return value
    return _display(diagnostics.get("trust_grade"))


def _score(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    trade_idea = symbol_result.trade_idea
    if trade_idea is not None:
        value = _display(getattr(trade_idea, "confidence_score", NA))
        if value != NA:
            return value
    quality = symbol_result.setup_quality
    if getattr(quality, "is_evaluated", False):
        value = _display(getattr(quality, "quality_score", NA))
        if value != NA:
            return value
    return _display(diagnostics.get("trust_percentage"))


def _admin_action_suggestion(display_status: str, failed_stage: str, fields: Mapping[str, Any]) -> str:
    if failed_stage == "target_integrity":
        return "Do not alert; wait for a clean target path."
    if fields.get("lifecycle_integrity_status") == "STALE_OR_DEGRADED":
        return "Review stale lifecycle state before any manual action."
    if display_status == "valid_setup":
        return "Admin review only; prepare manual alert draft if still valid."
    if display_status == "near_miss":
        return "Watch only; wait for next trigger before review."
    return "No valid setup. No trade."


def _is_target_blocked_summary(summary: Mapping[str, Any]) -> bool:
    return summary.get("failed_stage") == "target_integrity" or summary.get("failed_gate") == "target_integrity"


def _manifest_value(manifest_row: Mapping[str, Any] | None, key: str, fallback: Any) -> Any:
    if manifest_row is None:
        return fallback
    value = manifest_row.get(key, fallback)
    if value is None or value == "":
        return fallback
    return value


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _display(value)
        if text != NA:
            return text
    return NA


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _safe_filename(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)
    return cleaned.strip("_") or "unknown_run"


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ADMIN_DRAFT_DELIVERY_STATUSES",
    "ADMIN_DRAFT_TYPES",
    "DEFAULT_ADMIN_DRAFTS_DIR",
    "AdminDraftPersistenceResult",
    "AdminDraftRecord",
    "AdminDraftRoutingResult",
    "AdminDraftType",
    "admin_draft_path_for_run",
    "build_admin_drafts",
    "format_admin_scan_report",
    "format_blocked_report",
    "format_lastscan_report",
    "format_near_report",
    "format_status_report",
    "persist_admin_drafts",
    "route_admin_scan_report",
]
