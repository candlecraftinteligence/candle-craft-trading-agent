from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.data.dtos import NA

ADMIN_COMMANDS: tuple[str, ...] = ("/start", "/help", "/status", "/lastscan", "/near", "/blocked")
DEFAULT_SCAN_RUN_MANIFEST_PATH = Path("scan_runs") / "scan_run_manifest.jsonl"
DEFAULT_ADMIN_COMMAND_ROW_LIMIT = 5


@dataclass(frozen=True)
class AdminCommandResponse:
    command: str
    response_type: str
    text: str
    run_id: str = NA


@dataclass(frozen=True)
class LatestScanArtifacts:
    manifest_row: Mapping[str, Any] | None
    scan_payload: Mapping[str, Any] | None
    scan_path: Path | None


class TelegramAdminCommandService:
    """Format admin-only Telegram command responses from local scan artifacts."""

    def __init__(
        self,
        *,
        project_root: Path | str = Path("."),
        manifest_path: Path | str = DEFAULT_SCAN_RUN_MANIFEST_PATH,
        max_rows: int = DEFAULT_ADMIN_COMMAND_ROW_LIMIT,
    ) -> None:
        self._project_root = Path(project_root)
        self._manifest_path = self._resolve_project_path(manifest_path)
        self._max_rows = max(1, max_rows)

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    def response_for(self, command: str) -> AdminCommandResponse:
        normalized = normalize_admin_command(command)
        if normalized == "/start":
            return AdminCommandResponse(normalized, "start", format_start_response())
        if normalized == "/help":
            return AdminCommandResponse(normalized, "help", format_help_response())
        if normalized == "/status":
            return self._status_response()
        if normalized == "/lastscan":
            return self._lastscan_response()
        if normalized == "/near":
            return self._near_response()
        if normalized == "/blocked":
            return self._blocked_response()
        return AdminCommandResponse(
            command=normalized,
            response_type="unknown",
            text="\n".join(
                (
                    "Unknown admin command.",
                    "Use /help for available commands.",
                    "Admin-only. No public/VIP posting. No execution enabled.",
                )
            ),
        )

    def latest_manifest_row(self) -> Mapping[str, Any] | None:
        return load_latest_manifest_row(self._manifest_path)

    def latest_scan_artifacts(self) -> LatestScanArtifacts:
        manifest_row = self.latest_manifest_row()
        if manifest_row is None:
            return LatestScanArtifacts(manifest_row=None, scan_payload=None, scan_path=None)
        scan_path_text = _display(manifest_row.get("latest_scan_path"))
        if scan_path_text == NA:
            return LatestScanArtifacts(manifest_row=manifest_row, scan_payload=None, scan_path=None)
        scan_path = self._resolve_project_path(scan_path_text)
        if not scan_path.exists():
            return LatestScanArtifacts(manifest_row=manifest_row, scan_payload=None, scan_path=scan_path)
        try:
            payload = json.loads(scan_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return LatestScanArtifacts(manifest_row=manifest_row, scan_payload=None, scan_path=scan_path)
        if not isinstance(payload, Mapping):
            return LatestScanArtifacts(manifest_row=manifest_row, scan_payload=None, scan_path=scan_path)
        return LatestScanArtifacts(manifest_row=manifest_row, scan_payload=payload, scan_path=scan_path)

    def _status_response(self) -> AdminCommandResponse:
        manifest_row = self.latest_manifest_row()
        run_id = _display(manifest_row.get("run_id")) if manifest_row is not None else NA
        if manifest_row is None:
            lines = [
                "Candle Craft Admin Status",
                "Admin-only.",
                f"Latest manifest: N/A ({self._manifest_path})",
                "No scan manifest rows found.",
                "Telegram mode: admin-only command scaffold. No public/VIP posting. No execution enabled.",
            ]
            return AdminCommandResponse("/status", "status", "\n".join(lines), run_id=NA)

        lines = [
            "Candle Craft Admin Status",
            "Admin-only.",
            f"Latest run_id: {_display(manifest_row.get('run_id'))}",
            f"Timestamp: {_display(manifest_row.get('timestamp'))}",
            f"Universe: {_universe_text(manifest_row)}",
            f"Market regime: {_display(manifest_row.get('market_regime'))}",
            f"Regime confidence: {_display(manifest_row.get('regime_confidence'))}",
            f"Symbols scanned: {_display(manifest_row.get('symbols_scanned'))}",
            f"Valid setups: {_display(manifest_row.get('valid_setup_count'))}",
            f"Near misses: {_display(manifest_row.get('near_miss_count'))}",
            f"Rejected: {_display(manifest_row.get('rejected_count'))}",
            f"Target-integrity blocked: {_display(manifest_row.get('alerts_blocked_by_target_integrity'))}",
            f"Alerts created: {_display(manifest_row.get('alerts_created'))}",
            f"Trade ideas created: {_display(manifest_row.get('trade_ideas_created'))}",
            f"Journal entries created: {_display(manifest_row.get('journal_entries_created'))}",
            f"Runtime seconds: {_display(manifest_row.get('runtime_seconds'))}",
            f"Latest scan path: {_display(manifest_row.get('latest_scan_path'))}",
            "Telegram mode: admin-only command scaffold. No public/VIP posting. No execution enabled.",
        ]
        return AdminCommandResponse("/status", "status", "\n".join(lines), run_id=run_id)

    def _lastscan_response(self) -> AdminCommandResponse:
        artifacts = self.latest_scan_artifacts()
        if artifacts.manifest_row is None:
            return _missing_scan_response(
                "/lastscan",
                "lastscan",
                f"No scan manifest rows found at {self._manifest_path}.",
            )
        if artifacts.scan_payload is None:
            return _missing_scan_response(
                "/lastscan",
                "lastscan",
                f"Latest scan file unavailable: {_display(artifacts.scan_path)}.",
                run_id=_display(artifacts.manifest_row.get("run_id")),
            )

        payload = artifacts.scan_payload
        manifest_row = artifacts.manifest_row
        rows = _result_rows(payload)
        valid_rows = _valid_rows(rows)
        near_rows = _near_rows(rows)
        summary_rows = valid_rows if valid_rows else near_rows
        section_title = "Top Valid Setups" if valid_rows else "Top Near Misses"
        lines = [
            "Candle Craft Latest Scan",
            "Admin-only.",
            f"Run: {_first_text(manifest_row.get('run_id'), payload.get('run_id'))}",
            f"Timestamp: {_display(manifest_row.get('timestamp'))}",
            f"Universe: {_universe_text(manifest_row, payload)}",
            f"Regime: {_first_text(manifest_row.get('market_regime'), _nested_value(payload, 'market_regime', 'state'))}",
            f"Symbols scanned: {_first_text(manifest_row.get('symbols_scanned'), payload.get('scanned_symbols'), len(rows))}",
            f"Valid setups: {_first_text(manifest_row.get('valid_setup_count'), len(valid_rows))}",
            f"Near misses: {_first_text(manifest_row.get('near_miss_count'), len(near_rows))}",
            f"Rejected: {_first_text(manifest_row.get('rejected_count'), _rejected_count(rows))}",
            section_title,
            *_setup_lines(summary_rows, max_rows=self._max_rows),
            "No execution enabled.",
        ]
        return AdminCommandResponse(
            "/lastscan",
            "lastscan",
            "\n".join(lines),
            run_id=_first_text(manifest_row.get("run_id"), payload.get("run_id")),
        )

    def _near_response(self) -> AdminCommandResponse:
        artifacts = self.latest_scan_artifacts()
        if artifacts.manifest_row is None:
            return _missing_scan_response("/near", "near", f"No scan manifest rows found at {self._manifest_path}.")
        if artifacts.scan_payload is None:
            return _missing_scan_response(
                "/near",
                "near",
                f"Latest scan file unavailable: {_display(artifacts.scan_path)}.",
                run_id=_display(artifacts.manifest_row.get("run_id")),
            )
        rows = _near_rows(_result_rows(artifacts.scan_payload))
        lines = [
            "Candle Craft Near Misses",
            "Admin-only.",
            f"Run: {_first_text(artifacts.manifest_row.get('run_id'), artifacts.scan_payload.get('run_id'))}",
            *_near_lines(rows, max_rows=self._max_rows),
            "No execution enabled.",
        ]
        return AdminCommandResponse(
            "/near",
            "near",
            "\n".join(lines),
            run_id=_first_text(artifacts.manifest_row.get("run_id"), artifacts.scan_payload.get("run_id")),
        )

    def _blocked_response(self) -> AdminCommandResponse:
        artifacts = self.latest_scan_artifacts()
        if artifacts.manifest_row is None:
            return _missing_scan_response(
                "/blocked",
                "blocked",
                f"No scan manifest rows found at {self._manifest_path}.",
            )
        if artifacts.scan_payload is None:
            return _missing_scan_response(
                "/blocked",
                "blocked",
                f"Latest scan file unavailable: {_display(artifacts.scan_path)}.",
                run_id=_display(artifacts.manifest_row.get("run_id")),
            )
        rows = _blocked_rows(_result_rows(artifacts.scan_payload))
        if not rows:
            text = "\n".join(
                (
                    "Candle Craft Target-Integrity Blocks",
                    "Admin-only.",
                    f"Run: {_first_text(artifacts.manifest_row.get('run_id'), artifacts.scan_payload.get('run_id'))}",
                    "No target-integrity blocks in latest scan.",
                    "No execution enabled.",
                )
            )
        else:
            text = "\n".join(
                (
                    "Candle Craft Target-Integrity Blocks",
                    "Admin-only.",
                    f"Run: {_first_text(artifacts.manifest_row.get('run_id'), artifacts.scan_payload.get('run_id'))}",
                    *_blocked_lines(rows, max_rows=self._max_rows),
                    "No execution enabled.",
                )
            )
        return AdminCommandResponse(
            "/blocked",
            "blocked",
            text,
            run_id=_first_text(artifacts.manifest_row.get("run_id"), artifacts.scan_payload.get("run_id")),
        )

    def _resolve_project_path(self, value: Path | str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return self._project_root / path


def format_start_response() -> str:
    return "\n".join(
        (
            "Welcome to the Candle Craft Intelligence admin desk.",
            "Admin-only private bot chat.",
            _command_list_text(),
            "No public/VIP posting. No execution enabled.",
        )
    )


def format_help_response() -> str:
    return "\n".join(
        (
            "Candle Craft Admin Help",
            "Commands:",
            "/start - welcome and command list",
            "/help - command list and safety notes",
            "/status - latest scan metadata",
            "/lastscan - compact latest scan summary",
            "/near - top near misses",
            "/blocked - target-integrity blocked rows",
            "Admin-only. No public/VIP posting, subscriptions, payments, or Mini App. No execution enabled.",
        )
    )


def normalize_admin_command(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    token = text.split()[0].split("@", 1)[0].lower()
    return token


def load_latest_manifest_row(path: Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    latest: Mapping[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            latest = payload
    return latest


def _missing_scan_response(command: str, response_type: str, reason: str, *, run_id: str = NA) -> AdminCommandResponse:
    return AdminCommandResponse(
        command,
        response_type,
        "\n".join(
            (
                "Candle Craft Latest Scan",
                "Admin-only.",
                f"Run: {run_id}",
                reason,
                "No scan data returned.",
                "No execution enabled.",
            )
        ),
        run_id=run_id,
    )


def _command_list_text() -> str:
    return "Available commands: " + ", ".join(ADMIN_COMMANDS)


def _result_rows(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    rows = payload.get("results")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return ()
    return tuple(row for row in rows if isinstance(row, Mapping))


def _valid_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(row for row in rows if _display_status(row) == "valid_setup" or _display(row.get("display_bucket")) == "valid")


def _near_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(
        row
        for row in rows
        if (_display_status(row) == "near_miss" or _display(row.get("display_bucket")) == "near_miss")
        and not _is_target_integrity_blocked(row)
    )


def _blocked_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(row for row in rows if _is_target_integrity_blocked(row))


def _ranked_rows(rows: Any) -> tuple[Mapping[str, Any], ...]:
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda item: (_rank_value(item[1]), item[0]))
    return tuple(row for _index, row in indexed)


def _rank_value(row: Mapping[str, Any]) -> int:
    value = row.get("display_rank")
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 1_000_000


def _setup_lines(rows: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    if not rows:
        return ["- None"]
    lines = [
        (
            f"- {_display(row.get('symbol'))} | side {_row_side(row)} | grade {_row_grade(row)} | "
            f"score {_row_score(row)} | {_row_short_reason(row)} | next {_row_next_trigger(row)}"
        )
        for row in rows[:max_rows]
    ]
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _near_lines(rows: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    if not rows:
        return ["- No near misses in latest scan."]
    lines = [
        (
            f"- {_display(row.get('symbol'))} | side {_row_side(row)} | grade {_row_grade(row)} | "
            f"score {_row_score(row)} | failed_stage {_row_failed_stage(row)} | "
            f"{_row_short_reason(row)} | next {_row_next_trigger(row)} | "
            f"lifecycle {_display(row.get('lifecycle_integrity_status'))}"
        )
        for row in rows[:max_rows]
    ]
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _blocked_lines(rows: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    lines = [
        (
            f"- {_display(row.get('symbol'))} | side {_row_side(row)} | "
            f"failure {_target_failure_type(row)} | reason {_target_reason(row)} | "
            f"short {_row_short_reason(row)} | next {_row_next_trigger(row)} | "
            f"state {_display(row.get('lifecycle_current_state'))} | "
            f"lifecycle {_display(row.get('lifecycle_integrity_status'))}"
        )
        for row in rows[:max_rows]
    ]
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _with_omitted(lines: list[str], *, total: int, max_rows: int) -> list[str]:
    if total > max_rows:
        lines.append(f"- {total - max_rows} more omitted")
    return lines


def _rejected_count(rows: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for row in rows if _display(row.get("display_bucket")) == "no_setup" or _display_status(row) == "no_setup")


def _display_status(row: Mapping[str, Any]) -> str:
    status = _display(row.get("display_status"))
    if status != NA:
        return status
    if row.get("trade_idea") is not None or _display(row.get("status")) == "idea_created":
        return "valid_setup"
    return NA


def _is_target_integrity_blocked(row: Mapping[str, Any]) -> bool:
    return any(
        _display(value) == "target_integrity"
        for value in (row.get("failed_stage"), row.get("failed_gate"), row.get("rejection_stage"))
    ) or _display(row.get("target_integrity_status")).lower() == "blocked"


def _row_side(row: Mapping[str, Any]) -> str:
    return _first_text(
        row.get("side"),
        row.get("direction"),
        _nested_value(row, "trade_idea", "direction"),
        _diagnostic_value(row, "direction"),
        _diagnostic_value(row, "side"),
        _diagnostic_value(row, "bias"),
    )


def _row_grade(row: Mapping[str, Any]) -> str:
    return _first_text(
        row.get("grade"),
        _nested_value(row, "trade_idea", "grade"),
        _nested_value(row, "setup_quality", "quality_grade"),
        _diagnostic_value(row, "trust_grade"),
    )


def _row_score(row: Mapping[str, Any]) -> str:
    return _first_text(
        row.get("score"),
        _nested_value(row, "trade_idea", "confidence_score"),
        _nested_value(row, "setup_quality", "quality_score"),
        _diagnostic_value(row, "trust_percentage"),
        _diagnostic_value(row, "trust_score"),
    )


def _row_failed_stage(row: Mapping[str, Any]) -> str:
    return _first_text(row.get("failed_stage"), row.get("rejection_stage"), _diagnostic_value(row, "first_failed_gate"))


def _row_short_reason(row: Mapping[str, Any]) -> str:
    return _first_text(
        row.get("short_reason"),
        row.get("display_reason"),
        row.get("rejection_reason"),
        _diagnostic_value(row, "target_integrity_reason"),
    )


def _row_next_trigger(row: Mapping[str, Any]) -> str:
    return _first_text(
        row.get("next_trigger_needed"),
        _nested_value(row, "near_miss_intelligence", "next_trigger_needed"),
        _nested_value(row, "target_intelligence", "next_target_condition"),
    )


def _target_failure_type(row: Mapping[str, Any]) -> str:
    return _first_text(
        row.get("target_integrity_failure_type"),
        row.get("target_failure_type"),
        _nested_value(row, "target_intelligence", "target_failure_type"),
        _diagnostic_value(row, "target_failure_type"),
    )


def _target_reason(row: Mapping[str, Any]) -> str:
    return _first_text(
        row.get("target_integrity_reason"),
        row.get("target_integrity_warning"),
        row.get("rr_compression_reason"),
        _nested_value(row, "target_intelligence", "rr_compression_reason"),
        _nested_value(row, "target_intelligence", "next_target_condition"),
        _diagnostic_value(row, "target_integrity_reason"),
    )


def _diagnostic_value(row: Mapping[str, Any], key: str) -> Any:
    diagnostics = row.get("strategy_diagnostics")
    if not isinstance(diagnostics, Mapping):
        return NA
    for mode_key in _diagnostic_mode_order(row):
        value = diagnostics.get(mode_key)
        if isinstance(value, Mapping) and _display(value.get(key)) != NA:
            return value.get(key)
    for value in diagnostics.values():
        if isinstance(value, Mapping) and _display(value.get(key)) != NA:
            return value.get(key)
    return NA


def _diagnostic_mode_order(row: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("valid_strategy_modes", "rejected_strategy_modes"):
        modes = row.get(key)
        if isinstance(modes, Sequence) and not isinstance(modes, (str, bytes)):
            values.extend(_display(mode) for mode in modes if _display(mode) != NA)
    values.extend(["challenge", "swing", "scalp"])
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return tuple(deduped)


def _universe_text(*sources: Mapping[str, Any]) -> str:
    for source in sources:
        label = _first_text(source.get("universe_label"), _nested_value(source, "universe", "label"))
        mode = _first_text(source.get("universe_mode"), _nested_value(source, "universe", "mode"))
        if label != NA and mode != NA:
            return f"{label} ({mode})"
        if label != NA:
            return label
        if mode != NA:
            return mode
    return NA


def _nested_value(source: Mapping[str, Any], key: str, nested_key: str) -> Any:
    value = source.get(key)
    if isinstance(value, Mapping):
        return value.get(nested_key, NA)
    return NA


def _first_text(*values: Any) -> str:
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
    return str(value)


__all__ = [
    "ADMIN_COMMANDS",
    "DEFAULT_ADMIN_COMMAND_ROW_LIMIT",
    "DEFAULT_SCAN_RUN_MANIFEST_PATH",
    "AdminCommandResponse",
    "LatestScanArtifacts",
    "TelegramAdminCommandService",
    "format_help_response",
    "format_start_response",
    "load_latest_manifest_row",
    "normalize_admin_command",
]
