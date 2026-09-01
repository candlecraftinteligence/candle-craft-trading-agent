from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.data.dtos import NA

ALERT_INTEGRITY_MANIFEST_SCHEMA_VERSION = "alert_integrity_manifest_v1"
ALERT_INTEGRITY_AUDIT_SCHEMA_VERSION = "alert_integrity_audit_v1"
ALERT_INTEGRITY_SAFETY_NOTE = (
    "Alert integrity manifests are audit metadata only. They do not create signals, place orders, call "
    "exchanges, send Telegram messages, weaken setup gates, infer market data, withdraw funds, or transfer funds."
)

IssueSeverity = Literal["info", "warning", "blocker", "error"]

REQUIRED_TRADE_IDEA_FIELDS = (
    "symbol",
    "direction",
    "timeframe",
    "setup_type",
    "entry_zone",
    "stop_loss",
    "invalidation",
    "take_profits",
    "cancel_condition",
    "risk_warning",
)
FORBIDDEN_ALERT_KEY_FRAGMENTS = (
    "api_secret",
    "api_key",
    "bot_token",
    "chat_id",
    "order_id",
    "private_key",
    "transfer",
    "withdraw",
)


class AlertIntegrityIssue(BaseModel):
    severity: IssueSeverity
    code: str
    message: str
    path: str = "root"
    field_name: str = NA

    model_config = ConfigDict(frozen=True)


class AlertIntegrityManifest(BaseModel):
    schema_version: str = ALERT_INTEGRITY_MANIFEST_SCHEMA_VERSION
    manifest_id: str
    payload_sha256: str
    message_sha256: str
    trade_idea_sha256: str = NA
    message_part_sha256: tuple[str, ...] = ()
    channel: str = NA
    status: str = NA
    dry_run: bool = True
    deduplication_key: str = NA
    message_length: int = 0
    message_part_count: int = 0
    required_field_status: dict[str, bool] = Field(default_factory=dict)
    safety_checks: dict[str, bool] = Field(default_factory=dict)
    warning_count: int = 0
    blocker_count: int = 0
    error_count: int = 0
    is_valid: bool = True
    issues: tuple[AlertIntegrityIssue, ...] = ()
    safety_note: str = ALERT_INTEGRITY_SAFETY_NOTE

    model_config = ConfigDict(frozen=True)


class AlertIntegrityAuditRecord(BaseModel):
    path: str
    symbol: str = NA
    status: str = NA
    channel: str = NA
    dry_run: bool = True
    has_manifest: bool = False
    manifest_id: str = NA
    manifest_valid: bool = False
    message_sha256: str = NA
    issue_count: int = 0
    warning_count: int = 0
    blocker_count: int = 0
    error_count: int = 0
    issues: tuple[AlertIntegrityIssue, ...] = ()

    model_config = ConfigDict(frozen=True)


class AlertIntegrityAuditSummary(BaseModel):
    alert_count: int = 0
    manifest_count: int = 0
    missing_manifest_count: int = 0
    dry_run_alerts: int = 0
    live_alerts: int = 0
    invalid_alerts: int = 0
    warning_count: int = 0
    blocker_count: int = 0
    error_count: int = 0
    is_valid: bool = True

    model_config = ConfigDict(frozen=True)


class AlertIntegrityAuditResult(BaseModel):
    source: str
    schema_version: str = ALERT_INTEGRITY_AUDIT_SCHEMA_VERSION
    summary: AlertIntegrityAuditSummary = Field(default_factory=AlertIntegrityAuditSummary)
    records: tuple[AlertIntegrityAuditRecord, ...] = ()
    issues: tuple[AlertIntegrityIssue, ...] = ()
    safety_note: str = ALERT_INTEGRITY_SAFETY_NOTE

    model_config = ConfigDict(frozen=True)


def build_alert_integrity_manifest(
    *,
    trade_idea: Any,
    formatted_message: str,
    message_parts: Sequence[str],
    channel: Any,
    status: Any,
    dry_run: bool,
    deduplication_key: str | None = None,
) -> AlertIntegrityManifest:
    trade_data = _as_mapping(trade_idea)
    issues: list[AlertIntegrityIssue] = []
    required_field_status = _required_field_status(trade_data)

    if trade_data is None:
        _add_issue(
            issues,
            "blocker",
            "missing_trade_idea",
            "Alert integrity cannot be fully validated without the trade idea payload.",
        )
        trade_data = {}

    for field_name, present in required_field_status.items():
        if not present:
            _add_issue(
                issues,
                "blocker",
                f"missing_{field_name}",
                f"Trade idea field {field_name} is required for alert integrity.",
                f"trade_idea.{field_name}",
                field_name,
            )

    quality_gate = _quality_gate_passed(trade_data)
    if quality_gate is False:
        _add_issue(
            issues,
            "blocker",
            "trade_idea_quality_gate_failed",
            "Alert trade idea quality gate did not pass.",
            "trade_idea.quality_gate_result.passed",
            "quality_gate_result",
        )
    elif quality_gate is None:
        _add_issue(
            issues,
            "warning",
            "quality_gate_unavailable",
            "Trade idea quality gate status is unavailable in the alert payload.",
            "trade_idea.quality_gate_result",
            "quality_gate_result",
        )

    if _status_key(trade_data.get("status")) == "rejected":
        _add_issue(
            issues,
            "blocker",
            "rejected_trade_idea_alert",
            "Rejected trade ideas must not be treated as alertable setups.",
            "trade_idea.status",
            "status",
        )

    message = "" if formatted_message is None else str(formatted_message)
    if not message.strip():
        _add_issue(issues, "error", "empty_alert_message", "Alert message is empty.", "formatted_message")
    message_has_risk_warning = _message_has_risk_warning(message)
    message_has_invalidation = _message_has_invalidation(message)
    if not message_has_risk_warning:
        _add_issue(
            issues,
            "blocker",
            "message_missing_risk_warning",
            "Alert message must include a risk warning.",
            "formatted_message",
            "risk_warning",
        )
    if not message_has_invalidation:
        _add_issue(
            issues,
            "blocker",
            "message_missing_invalidation",
            "Alert message must include invalidation.",
            "formatted_message",
            "invalidation",
        )

    if not _present(deduplication_key):
        _add_issue(
            issues,
            "warning",
            "missing_deduplication_key",
            "Alert has no deduplication key, so downstream duplicate detection is weaker.",
            "deduplication_key",
            "deduplication_key",
        )
    if not dry_run:
        _add_issue(
            issues,
            "warning",
            "live_alert_delivery",
            "Alert is configured for live notification delivery; no order execution is implied or added.",
            "dry_run",
            "dry_run",
        )

    forbidden_keys = _forbidden_keys(trade_data)
    for key_path in forbidden_keys:
        _add_issue(
            issues,
            "blocker",
            "forbidden_alert_key_present",
            "Alert payload contains a forbidden secret, execution, withdrawal, or transfer-like key.",
            key_path,
        )

    parts = tuple(str(part) for part in message_parts)
    if not parts:
        _add_issue(issues, "error", "missing_message_parts", "Alert has no message parts.", "message_parts")

    channel_text = _text(channel)
    status_text = _text(status)
    dedupe_text = _text(deduplication_key)
    message_sha = _sha256(message)
    part_hashes = tuple(_sha256(part) for part in parts)
    trade_sha = _digest_json(trade_data) if trade_data else NA
    safety_checks = {
        "deduplication_key_present": _present(deduplication_key),
        "dry_run": dry_run,
        "invalidation_present": required_field_status.get("invalidation", False),
        "message_has_invalidation": message_has_invalidation,
        "message_has_risk_warning": message_has_risk_warning,
        "no_forbidden_alert_keys": not forbidden_keys,
        "risk_warning_present": required_field_status.get("risk_warning", False),
    }
    payload_sha = _digest_json(
        {
            "channel": channel_text,
            "deduplication_key": dedupe_text,
            "dry_run": dry_run,
            "message_part_sha256": part_hashes,
            "message_sha256": message_sha,
            "required_field_status": required_field_status,
            "status": status_text,
            "trade_idea_sha256": trade_sha,
        }
    )
    counts = Counter(issue.severity for issue in issues)
    return AlertIntegrityManifest(
        manifest_id=f"aim-{payload_sha[:16]}",
        payload_sha256=payload_sha,
        message_sha256=message_sha,
        trade_idea_sha256=trade_sha,
        message_part_sha256=part_hashes,
        channel=channel_text,
        status=status_text,
        dry_run=dry_run,
        deduplication_key=dedupe_text,
        message_length=len(message),
        message_part_count=len(parts),
        required_field_status=required_field_status,
        safety_checks=safety_checks,
        warning_count=counts["warning"],
        blocker_count=counts["blocker"],
        error_count=counts["error"],
        is_valid=counts["blocker"] == 0 and counts["error"] == 0,
        issues=tuple(issues),
    )


def audit_alert_integrity_artifact(data: Any, source: str = "in_memory") -> AlertIntegrityAuditResult:
    records: list[AlertIntegrityAuditRecord] = []
    issues: list[AlertIntegrityIssue] = []

    for alert_ref in _iter_alert_references(data):
        records.append(_audit_alert_reference(alert_ref))

    if not records:
        _add_issue(issues, "info", "no_alerts_found", "No alert result records were found in the artifact.")

    all_issues = tuple(issues) + tuple(issue for record in records for issue in record.issues)
    counts = Counter(issue.severity for issue in all_issues)
    summary = AlertIntegrityAuditSummary(
        alert_count=len(records),
        manifest_count=sum(1 for record in records if record.has_manifest),
        missing_manifest_count=sum(1 for record in records if not record.has_manifest),
        dry_run_alerts=sum(1 for record in records if record.dry_run),
        live_alerts=sum(1 for record in records if not record.dry_run),
        invalid_alerts=sum(1 for record in records if record.blocker_count or record.error_count),
        warning_count=counts["warning"],
        blocker_count=counts["blocker"],
        error_count=counts["error"],
        is_valid=counts["blocker"] == 0 and counts["error"] == 0,
    )
    return AlertIntegrityAuditResult(
        source=_text(source),
        summary=summary,
        records=tuple(records),
        issues=all_issues,
    )


def audit_alert_integrity_file(path: Path) -> AlertIntegrityAuditResult:
    artifact_path = Path(path)
    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        issue = AlertIntegrityIssue(
            severity="error",
            code="invalid_json",
            message=f"JSON could not be decoded: {exc.msg}",
            path="root",
        )
        return _audit_result_for_input_error(str(artifact_path), issue)
    except OSError as exc:
        issue = AlertIntegrityIssue(
            severity="error",
            code="unreadable_json",
            message=f"Artifact could not be read: {exc}",
            path="root",
        )
        return _audit_result_for_input_error(str(artifact_path), issue)
    return audit_alert_integrity_artifact(data, source=str(artifact_path))


def audit_alert_integrity_files(paths: Sequence[Path]) -> AlertIntegrityAuditResult:
    records: list[AlertIntegrityAuditRecord] = []
    issues: list[AlertIntegrityIssue] = []
    sources: list[str] = []
    for path in paths:
        result = audit_alert_integrity_file(path)
        sources.append(result.source)
        records.extend(result.records)
        issues.extend(result.issues)

    if not paths:
        _add_issue(issues, "info", "no_input_artifacts", "No default alert artifacts were found.")

    all_issues = tuple(issues)
    counts = Counter(issue.severity for issue in all_issues)
    summary = AlertIntegrityAuditSummary(
        alert_count=len(records),
        manifest_count=sum(1 for record in records if record.has_manifest),
        missing_manifest_count=sum(1 for record in records if not record.has_manifest),
        dry_run_alerts=sum(1 for record in records if record.dry_run),
        live_alerts=sum(1 for record in records if not record.dry_run),
        invalid_alerts=sum(1 for record in records if record.blocker_count or record.error_count),
        warning_count=counts["warning"],
        blocker_count=counts["blocker"],
        error_count=counts["error"],
        is_valid=counts["blocker"] == 0 and counts["error"] == 0,
    )
    return AlertIntegrityAuditResult(
        source=", ".join(sources) if sources else "N/A",
        summary=summary,
        records=tuple(records),
        issues=all_issues,
    )


def alert_integrity_audit_to_dict(result: AlertIntegrityAuditResult) -> dict[str, Any]:
    return _jsonable(result.model_dump(mode="python"))


def _audit_alert_reference(alert_ref: Mapping[str, Any]) -> AlertIntegrityAuditRecord:
    alert = _as_mapping(alert_ref.get("alert_result")) or {}
    trade_idea = _as_mapping(alert_ref.get("trade_idea"))
    path = _text(alert_ref.get("path"))
    issues: list[AlertIntegrityIssue] = []
    manifest_data = _as_mapping(alert.get("integrity_manifest"))
    has_manifest = manifest_data is not None
    formatted_message = _alert_message(alert)
    message_parts = _alert_message_parts(alert, formatted_message)
    status = _first_non_na(alert.get("status"), alert.get("delivery_status"), alert_ref.get("status"))
    channel = _first_non_na(alert.get("channel"), alert_ref.get("channel"))
    dry_run = _bool_from_alert(alert, status)
    deduplication_key = _first_non_na(alert.get("deduplication_key"), alert_ref.get("deduplication_key"))
    symbol = _first_non_na(
        alert_ref.get("symbol"),
        trade_idea.get("symbol") if trade_idea is not None else NA,
    )

    if not has_manifest:
        _add_issue(
            issues,
            "warning",
            "missing_integrity_manifest",
            "Alert result has no integrity_manifest metadata.",
            f"{path}.integrity_manifest",
        )
    else:
        issues.extend(_manifest_consistency_issues(manifest_data, alert, formatted_message, message_parts, path))

    if trade_idea is not None:
        expected = build_alert_integrity_manifest(
            trade_idea=trade_idea,
            formatted_message=formatted_message,
            message_parts=message_parts,
            channel=channel,
            status=status,
            dry_run=dry_run,
            deduplication_key=None if deduplication_key == NA else deduplication_key,
        )
        issues.extend(_issue_with_path(issue, path) for issue in expected.issues)
        if has_manifest and _text(manifest_data.get("payload_sha256")) != expected.payload_sha256:
            _add_issue(
                issues,
                "blocker",
                "manifest_payload_sha256_mismatch",
                "Stored manifest payload hash does not match the alert payload.",
                f"{path}.integrity_manifest.payload_sha256",
                "payload_sha256",
            )
    else:
        parent_status = _status_key(alert_ref.get("parent_status"))
        if parent_status in {"alert_dry_run_created", "journal_entry_created"} or "alert_dry_run_created" in _status_history(
            alert_ref.get("status_history")
        ):
            _add_issue(
                issues,
                "blocker",
                "alert_without_trade_idea",
                "Scanner alert result is present without its trade_idea context.",
                path,
                "trade_idea",
            )

    parent_status = _status_key(alert_ref.get("parent_status"))
    if parent_status.startswith("rejected") or parent_status in {"scanned_no_setup", "scan_error"}:
        _add_issue(
            issues,
            "blocker",
            "alert_on_non_alertable_status",
            "Alert result is attached to a rejected, no-setup, or scan-error scanner result.",
            path,
            "status",
        )

    history = _status_history(alert_ref.get("status_history"))
    if history and "alert_dry_run_created" in history and "idea_created" not in history:
        _add_issue(
            issues,
            "blocker",
            "alert_without_idea_status_history",
            "Alert status history must include idea_created before alert_dry_run_created.",
            f"{path}.status_history",
            "status_history",
        )

    counts = Counter(issue.severity for issue in issues)
    manifest_valid = has_manifest and counts["blocker"] == 0 and counts["error"] == 0
    return AlertIntegrityAuditRecord(
        path=path,
        symbol=_uppercase(symbol),
        status=_text(status),
        channel=_text(channel),
        dry_run=dry_run,
        has_manifest=has_manifest,
        manifest_id=_text(manifest_data.get("manifest_id")) if manifest_data is not None else NA,
        manifest_valid=manifest_valid,
        message_sha256=_sha256(formatted_message) if formatted_message != NA else NA,
        issue_count=len(issues),
        warning_count=counts["warning"],
        blocker_count=counts["blocker"],
        error_count=counts["error"],
        issues=tuple(issues),
    )


def _manifest_consistency_issues(
    manifest: Mapping[str, Any],
    alert: Mapping[str, Any],
    formatted_message: str,
    message_parts: Sequence[str],
    path: str,
) -> tuple[AlertIntegrityIssue, ...]:
    issues: list[AlertIntegrityIssue] = []
    if _text(manifest.get("schema_version")) != ALERT_INTEGRITY_MANIFEST_SCHEMA_VERSION:
        _add_issue(
            issues,
            "blocker",
            "manifest_schema_version_mismatch",
            "Alert integrity manifest schema version is unsupported.",
            f"{path}.integrity_manifest.schema_version",
            "schema_version",
        )
    if _text(manifest.get("message_sha256")) != _sha256(formatted_message):
        _add_issue(
            issues,
            "blocker",
            "manifest_message_sha256_mismatch",
            "Stored manifest message hash does not match the alert message.",
            f"{path}.integrity_manifest.message_sha256",
            "message_sha256",
        )
    if _safe_int(manifest.get("message_part_count"), -1) != len(message_parts):
        _add_issue(
            issues,
            "warning",
            "manifest_message_part_count_mismatch",
            "Stored manifest message part count does not match alert message_parts.",
            f"{path}.integrity_manifest.message_part_count",
            "message_part_count",
        )
    if _text(manifest.get("status")) != _text(alert.get("status", alert.get("delivery_status"))):
        _add_issue(
            issues,
            "warning",
            "manifest_status_mismatch",
            "Stored manifest status does not match alert status.",
            f"{path}.integrity_manifest.status",
            "status",
        )
    if bool(manifest.get("dry_run", True)) != _bool_from_alert(alert, alert.get("status")):
        _add_issue(
            issues,
            "warning",
            "manifest_dry_run_mismatch",
            "Stored manifest dry_run flag does not match alert delivery mode.",
            f"{path}.integrity_manifest.dry_run",
            "dry_run",
        )
    if manifest.get("is_valid") is False:
        _add_issue(
            issues,
            "blocker",
            "stored_manifest_invalid",
            "Stored alert integrity manifest reports invalid alert integrity.",
            f"{path}.integrity_manifest.is_valid",
            "is_valid",
        )
    return tuple(issues)


def _iter_alert_references(data: Any) -> tuple[dict[str, Any], ...]:
    refs: list[dict[str, Any]] = []

    def visit(value: Any, path: str, parent: Mapping[str, Any] | None = None) -> None:
        if isinstance(value, Mapping):
            if _looks_like_alert_result(value):
                refs.append(
                    {
                        "path": path,
                        "alert_result": value,
                        "trade_idea": parent.get("trade_idea") if parent is not None else None,
                        "symbol": parent.get("symbol") if parent is not None else value.get("symbol"),
                        "parent_status": parent.get("status") if parent is not None else value.get("status"),
                        "status_history": parent.get("status_history") if parent is not None else value.get("status_history"),
                    }
                )
                return
            if "alert_result" in value:
                alert_result = value.get("alert_result")
                if isinstance(alert_result, Mapping):
                    refs.append(
                        {
                            "path": f"{path}.alert_result" if path != "root" else "alert_result",
                            "alert_result": alert_result,
                            "trade_idea": value.get("trade_idea"),
                            "symbol": value.get("symbol"),
                            "parent_status": value.get("status"),
                            "status_history": value.get("status_history"),
                        }
                    )
                    return
                if alert_result not in (None, NA):
                    refs.append(
                        {
                            "path": f"{path}.alert_result" if path != "root" else "alert_result",
                            "alert_result": {"status": NA, "formatted_message": NA, "message_parts": ()},
                            "trade_idea": value.get("trade_idea"),
                            "symbol": value.get("symbol"),
                            "parent_status": value.get("status"),
                            "status_history": value.get("status_history"),
                        }
                    )
                    return
            if _status_key(value.get("status")) == "alert_dry_run_created" or "alert_dry_run_created" in _status_history(
                value.get("status_history")
            ):
                refs.append(
                    {
                        "path": path,
                        "alert_result": {"status": NA, "formatted_message": NA, "message_parts": ()},
                        "trade_idea": value.get("trade_idea"),
                        "symbol": value.get("symbol"),
                        "parent_status": value.get("status"),
                        "status_history": value.get("status_history"),
                    }
                )
                return
            for key, item in value.items():
                next_path = f"{path}.{key}" if path != "root" else str(key)
                visit(item, next_path, value)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]", parent)

    visit(data, "root")
    return tuple(refs)


def _looks_like_alert_result(value: Mapping[str, Any]) -> bool:
    keys = set(value)
    return bool(
        {"formatted_message", "message_parts", "delivery_results", "deduplication_key", "integrity_manifest"} & keys
        and {"status", "delivery_status"} & keys
    )


def _required_field_status(data: Mapping[str, Any] | None) -> dict[str, bool]:
    if data is None:
        return {field: False for field in REQUIRED_TRADE_IDEA_FIELDS}
    return {field: _field_present(data, field) for field in REQUIRED_TRADE_IDEA_FIELDS}


def _quality_gate_passed(data: Mapping[str, Any]) -> bool | None:
    quality_gate = data.get("quality_gate_result")
    if isinstance(quality_gate, Mapping):
        passed = quality_gate.get("passed")
        if isinstance(passed, bool):
            return passed
    return None


def _field_present(data: Mapping[str, Any], field_name: str) -> bool:
    value = data.get(field_name)
    if field_name == "entry_zone":
        return _level_present(value) or (_present(data.get("entry_low")) and _present(data.get("entry_high")))
    if field_name == "stop_loss":
        return _level_present(value)
    if field_name == "take_profits":
        return _sequence_present(value) or _sequence_present(data.get("take_profit_targets"))
    return _present(value)


def _level_present(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_present(value.get(field)) for field in ("price", "low", "high"))
    return _present(value)


def _sequence_present(value: Any) -> bool:
    if isinstance(value, str):
        return _present(value)
    if isinstance(value, Sequence):
        return any(_present(item) for item in value)
    return _present(value)


def _message_has_field(message: str, label: str) -> bool:
    prefix = f"{label}:"
    for line in message.splitlines():
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        return _present(value)
    return False


def _message_has_risk_warning(message: str) -> bool:
    if _message_has_field(message, "Risk warning"):
        return True
    for line in message.splitlines():
        text = line.strip().lower()
        if "no chase" in text:
            return True
        if "manual execution" in text and "manage risk" in text:
            return True
        if "not financial advice" in text:
            return True
    return False

def _message_has_invalidation(message: str) -> bool:
    if _message_has_field(message, "Invalidation"):
        return True
    lines = message.splitlines()
    for index, line in enumerate(lines):
        text = line.strip()
        if text.startswith("🛡 SL ") and _present(text.removeprefix("🛡 SL ").strip()):
            return True
        if "invalid if" in text.lower():
            return _next_non_blank_line_present(lines, index + 1)
    return False


def _next_non_blank_line_present(lines: Sequence[str], start: int) -> bool:
    for line in lines[start:]:
        text = line.strip()
        if not text:
            continue
        return _present(text)
    return False


def _forbidden_keys(value: Any, path: str = "trade_idea") -> tuple[str, ...]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            next_path = f"{path}.{key}"
            if any(fragment in key_text for fragment in FORBIDDEN_ALERT_KEY_FRAGMENTS):
                paths.append(next_path)
            paths.extend(_forbidden_keys(item, next_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_forbidden_keys(item, f"{path}[{index}]"))
    return tuple(paths)


def _alert_message(alert: Mapping[str, Any]) -> str:
    message = _first_non_na(alert.get("formatted_message"), alert.get("message"))
    return message if message != NA else ""


def _alert_message_parts(alert: Mapping[str, Any], formatted_message: str) -> tuple[str, ...]:
    parts = alert.get("message_parts")
    if isinstance(parts, Sequence) and not isinstance(parts, (str, bytes, bytearray, Mapping)):
        cleaned = tuple(str(part) for part in parts)
        return cleaned if cleaned else (() if formatted_message == "" else (formatted_message,))
    return () if formatted_message == "" else (formatted_message,)


def _bool_from_alert(alert: Mapping[str, Any], status: Any) -> bool:
    dry_run = alert.get("dry_run")
    if isinstance(dry_run, bool):
        return dry_run
    return _status_key(status) == "dry_run"


def _status_history(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (_status_key(value),)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, Mapping)):
        return tuple(_status_key(item) for item in value if _status_key(item))
    return ()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _issue_with_path(issue: AlertIntegrityIssue, path: str) -> AlertIntegrityIssue:
    issue_path = issue.path
    if issue_path == "root":
        issue_path = path
    elif not issue_path.startswith(path):
        issue_path = f"{path}.{issue_path}"
    return issue.model_copy(update={"path": issue_path})


def _audit_result_for_input_error(source: str, issue: AlertIntegrityIssue) -> AlertIntegrityAuditResult:
    summary = AlertIntegrityAuditSummary(error_count=1, is_valid=False)
    return AlertIntegrityAuditResult(source=source, summary=summary, issues=(issue,))


def _add_issue(
    issues: list[AlertIntegrityIssue],
    severity: IssueSeverity,
    code: str,
    message: str,
    path: str = "root",
    field_name: str = NA,
) -> None:
    issues.append(
        AlertIntegrityIssue(
            severity=severity,
            code=code,
            message=message,
            path=path,
            field_name=field_name,
        )
    )


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return {str(key): _jsonable(item) for key, item in dumped.items()}
    return None


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
    if hasattr(value, "value") and not isinstance(value, (int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    return str(value).strip()


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text != NA:
            return text
    return NA


def _uppercase(value: Any) -> str:
    text = _text(value)
    return text.upper() if text != NA else NA


def _status_key(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return ""
    key = text.strip().replace("-", "_").replace(" ", "_").lower()
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_json(value: Any) -> str:
    return _sha256(json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
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
    "ALERT_INTEGRITY_AUDIT_SCHEMA_VERSION",
    "ALERT_INTEGRITY_MANIFEST_SCHEMA_VERSION",
    "ALERT_INTEGRITY_SAFETY_NOTE",
    "AlertIntegrityAuditRecord",
    "AlertIntegrityAuditResult",
    "AlertIntegrityAuditSummary",
    "AlertIntegrityIssue",
    "AlertIntegrityManifest",
    "alert_integrity_audit_to_dict",
    "audit_alert_integrity_artifact",
    "audit_alert_integrity_file",
    "audit_alert_integrity_files",
    "build_alert_integrity_manifest",
]
