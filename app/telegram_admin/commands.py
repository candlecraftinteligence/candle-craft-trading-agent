from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.alerts.integrity_manifest import audit_alert_integrity_artifact
from app.data.dtos import NA
from app.formatters.telegram_signal_detail import (
    format_signal_detail,
    format_signal_detail_lifecycle,
    format_signal_detail_why_valid,
)
from app.formatters.telegram_signal_formatter import FOOTER as SIGNAL_FOOTER
from app.formatters.telegram_signal_formatter import HEADER_PREFIX as SIGNAL_HEADER_PREFIX
from app.formatters.telegram_wolf_briefing import build_wolf_briefing_snapshot, format_wolf_briefing
from app.storage.database import DEFAULT_DATABASE_PATH, StorageError, open_initialized_database
from app.telegram_admin.active_watchlists import (
    ACTIVE_WATCHLIST_DISPLAY_LIMIT,
    ActiveSignalItem,
    WATCHLIST_STAGE_DISPLAY_LIMIT,
    format_watchlist_stage_dashboard,
    load_active_public_signals,
    load_active_public_watchlists,
    load_watchlist_stage_dashboard,
)
from app.telegram_admin.signal_detail import load_active_signal_detail
from app.telegram_admin.wolf_briefing import WolfScanArtifacts, load_latest_db_scan_artifacts
from app.watchlists.presets import presets_with_counts

SCREEN_HEADER = "🐺🟠"
SCREEN_FOOTER = "🐺 Candle Craft | Signal. Structure. Execution."
SCREEN_DIVIDER = "━━━━━━━━━━━━━━━━━━"
UNVERIFIED = "Unverified"
JOIN_SIGNAL_CHANNEL_BUTTON_LABEL = "🐺 Join Signal Channel"
PUBLIC_SIGNAL_CHANNEL_COPY = (
    "Join the private Candle Craft signal channel for live watchlists and lifecycle updates."
)
PUBLIC_SIGNAL_CHANNEL_MISSING_COPY = "Signal channel invite link is not configured yet."
WOLF_BRIEFING_PREVIEW_HEADER = "🐺🟠 WOLF BRIEFING PREVIEW"
WOLF_BRIEFING_PUBLISH_COMMAND = "/wolf_publish"
WOLF_BRIEFING_CANCEL_COMMAND = "/wolf_cancel"
WOLF_BRIEFING_PUBLISH_BUTTON_LABEL = "📣 Send to Public Channel"
WOLF_BRIEFING_REFRESH_BUTTON_LABEL = "🔄 Refresh"
WOLF_BRIEFING_CANCEL_BUTTON_LABEL = "❌ Cancel"
WATCHLIST_REFRESH_BUTTON_LABEL = "🔄 Refresh"
WATCHLIST_BACK_BUTTON_LABEL = "⬅️ Back"
SIGNAL_DETAIL_REFRESH_BUTTON_LABEL = "🔄 Refresh"
SIGNAL_DETAIL_LIFECYCLE_BUTTON_LABEL = "📜 Lifecycle"
SIGNAL_DETAIL_WHY_VALID_BUTTON_LABEL = "🧠 Why valid?"
SIGNAL_DETAIL_BACK_BUTTON_LABEL = "⬅️ Back"
ADMIN_MENU_BUTTON_ROWS: tuple[tuple[str, ...], ...] = (
    ("🐺 Wolf Briefing", "📊 Status"),
    ("🚨 Alerts", "👁 Watchlist Desk"),
    ("🧾 Integrity", "⚙️ Config"),
    ("❓ Guide",),
)
ADMIN_MENU_BUTTON_COMMANDS: Mapping[str, str] = {
    "🐺 wolf briefing": "/wolf",
    "📊 status": "/status",
    "🚨 alerts": "/alerts",
    "👁 watchlist desk": "/watchlists",
    "👁 watchlists": "/watchlists",
    "🧾 integrity": "/integrity",
    "⚙️ config": "/config",
    "❓ guide": "/guide",
}
ADMIN_CALLBACK_COMMANDS: Mapping[str, str] = {
    "admin:wolf": "/wolf",
    "admin:status": "/status",
    "admin:alerts": "/alerts",
    "admin:watchlists": "/watchlists",
    "admin:integrity": "/integrity",
    "admin:config": "/config",
    "admin:guide": "/guide",
    "admin:menu": "/menu",
}
ADMIN_WOLF_BRIEFING_CALLBACK_COMMANDS: Mapping[str, str] = {
    "admin:wolf_publish": WOLF_BRIEFING_PUBLISH_COMMAND,
    "admin:wolf_refresh": "/wolf",
    "admin:wolf_cancel": WOLF_BRIEFING_CANCEL_COMMAND,
}
ADMIN_MENU_BUTTON_CALLBACKS: Mapping[str, str] = {
    "🐺 Wolf Briefing": "admin:wolf",
    "📊 Status": "admin:status",
    "🚨 Alerts": "admin:alerts",
    "👁 Watchlist Desk": "admin:watchlists",
    "🧾 Integrity": "admin:integrity",
    "⚙️ Config": "admin:config",
    "❓ Guide": "admin:guide",
}
PUBLIC_MENU_BUTTON_ROWS: tuple[tuple[str, ...], ...] = (
    ("📡 Last Scan", "🔥 Active Signals"),
    ("👁 Watchlists", "🌐 Social"),
    ("❓ Help", "🧡 Donate"),
)
PUBLIC_MENU_BUTTON_COMMANDS: Mapping[str, str] = {
    "📡 last scan": "/lastscan",
    "🔥 active signals": "/signals",
    "👁 watchlist": "/watchlists",
    "👁 watchlists": "/watchlists",
    "👁 watchlist signals": "/watchlists",
    "🌐 social": "/social",
    "❓ help": "/help",
    "🧡 donate": "/donate",
}
SIMPLE_REPLY_BUTTON_COMMANDS: Mapping[str, str] = {
    "🐺 menu": "/menu",
    "📡 status": "/status",
    "📊 latest alerts": "/latest",
    "ℹ️ about": "/about",
    "ℹ about": "/about",
    "active watchlists": "/watchlists",
}
PUBLIC_CALLBACK_COMMANDS: Mapping[str, str] = {
    "public:lastscan": "/lastscan",
    "public:signals": "/signals",
    "public:watchlist": "/watchlists",
    "public:social": "/social",
    "public:help": "/help",
    "public:donate": "/donate",
    "public:donate_usdt_ton": "/donate_usdt_ton",
    "public:donate_ton": "/donate_ton",
    "public:donate_btc": "/donate_btc",
    "public:menu": "/menu",
}
PUBLIC_MENU_BUTTON_CALLBACKS: Mapping[str, str] = {
    "📡 Last Scan": "public:lastscan",
    "🔥 Active Signals": "public:signals",
    "👁 Watchlists": "public:watchlist",
    "🌐 Social": "public:social",
    "❓ Help": "public:help",
    "🧡 Donate": "public:donate",
}
ADMIN_COMMANDS: tuple[str, ...] = (
    "/start",
    "/menu",
    "/status",
    "/wolf",
    "/latest",
    "/about",
    "/alerts",
    "/watchlists",
    "/signal",
    "/signal_lifecycle",
    "/signal_why",
    "/integrity",
    "/config",
    "/guide",
    "/help",
    "/lastscan",
    "/near",
    "/blocked",
    "/audit",
)
PUBLIC_COMMANDS: tuple[str, ...] = (
    "/start",
    "/menu",
    "/status",
    "/latest",
    "/about",
    "/lastscan",
    "/signals",
    "/watchlist",
    "/watchlists",
    "/signal",
    "/signal_lifecycle",
    "/signal_why",
    "/social",
    "/help",
    "/donate",
)
PUBLIC_ADMIN_RESERVED_COMMANDS: frozenset[str] = frozenset(
    {
        "/alerts",
        "/audit",
        "/integrity",
        "/config",
        "/wolf",
        "/near",
        "/blocked",
        "/guide",
    }
)
DEFAULT_SCAN_RUN_MANIFEST_PATH = Path("scan_runs") / "scan_run_manifest.jsonl"
DEFAULT_ADMIN_COMMAND_ROW_LIMIT = 5


@dataclass(frozen=True)
class AdminCommandResponse:
    command: str
    response_type: str
    text: str
    run_id: str = NA
    reply_markup: Mapping[str, Any] | None = None
    photo_path: Path | None = None
    photo_url: str | None = None
    cleanup_reply_keyboard: bool = False


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
        database_path: Path | str = DEFAULT_DATABASE_PATH,
        max_rows: int = DEFAULT_ADMIN_COMMAND_ROW_LIMIT,
    ) -> None:
        self._project_root = Path(project_root)
        self._manifest_path = self._resolve_project_path(manifest_path)
        self._database_path = self._resolve_project_path(database_path)
        self._max_rows = max(1, max_rows)

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    def response_for(self, command: str, *, admin_config: Any | None = None) -> AdminCommandResponse:
        normalized = normalize_admin_command(command)
        if normalized == "/start":
            return _admin_response(normalized, "start", format_start_response(), admin_config=admin_config)
        if normalized == "/menu":
            return _admin_response(normalized, "menu", format_menu_response(), admin_config=admin_config)
        if normalized in {"/help", "/guide"}:
            return _admin_response(normalized, "guide", format_help_response())
        if normalized == "/status":
            return self._status_response()
        if normalized == "/wolf":
            return self._wolf_briefing_response(admin_config=admin_config)
        if normalized == WOLF_BRIEFING_CANCEL_COMMAND:
            return _admin_response(
                normalized,
                "wolf_briefing_cancelled",
                _screen(
                    "Wolf Briefing",
                    (
                        "Wolf Briefing preview canceled.",
                        "",
                        SCREEN_DIVIDER,
                        "No public channel message was sent.",
                        SCREEN_DIVIDER,
                    ),
                ),
                admin_config=admin_config,
            )
        if normalized == "/latest":
            return self._latest_alerts_response()
        if normalized == "/about":
            return _admin_response(normalized, "about", format_about_response(), admin_config=admin_config)
        if normalized == "/alerts":
            return self._alerts_response()
        if normalized == "/watchlists":
            return self._watchlists_response()
        if normalized in {"/signal", "/signal_lifecycle", "/signal_why"}:
            return self._signal_detail_response(
                command,
                public=False,
                admin_config=admin_config,
            )
        if normalized in {"/integrity", "/audit"}:
            return self._integrity_response(normalized)
        if normalized == "/config":
            return self._config_response(admin_config=admin_config)
        if normalized == "/lastscan":
            return self._lastscan_response()
        if normalized == "/near":
            return self._near_response()
        if normalized == "/blocked":
            return self._blocked_response()
        return _admin_response(
            command=normalized,
            response_type="unknown",
            text=_screen(
                "Command Guide",
                (
                    "Unknown admin command.",
                    "Use /menu or /help for the command guide.",
                    SCREEN_DIVIDER,
                    "No execution.",
                    "Manual review only.",
                ),
            ),
        )

    def public_response_for(self, command: str, *, public_config: Any | None = None) -> AdminCommandResponse:
        normalized = normalize_admin_command(command)
        if normalized in {"/start", "/menu"}:
            photo_path = _public_logo_path(public_config, self._project_root) if normalized == "/start" else None
            return _public_response(
                normalized,
                "public_menu",
                format_public_menu_response(public_config),
                photo_path=photo_path,
                photo_url=(
                    _public_logo_url(public_config)
                    if normalized == "/start" and photo_path is None
                    else None
                ),
                public_config=public_config,
            )
        if normalized == "/status":
            return self._public_status_response(public_config=public_config)
        if normalized == "/latest":
            return self._public_latest_alerts_response()
        if normalized == "/about":
            return _public_response(
                normalized,
                "public_about",
                format_public_about_response(public_config),
                public_config=public_config,
            )
        if normalized == "/lastscan":
            return self._public_lastscan_response()
        if normalized == "/signals":
            return self._public_signals_response()
        if normalized in {"/signal", "/signal_lifecycle", "/signal_why"}:
            return self._signal_detail_response(command, public=True, public_config=public_config)
        if normalized in {"/watchlist", "/watchlists"}:
            return self._public_watchlist_response(command=normalized)
        if normalized == "/social":
            return _public_response(normalized, "public_social", format_public_social_response(public_config))
        if normalized == "/donate":
            return _public_response(
                normalized,
                "public_donate",
                format_public_donate_response(public_config),
                reply_markup=public_donate_inline_markup(public_config),
            )
        if normalized == "/donate_usdt_ton":
            return _public_response(
                normalized,
                "public_donate_usdt_ton",
                format_public_donate_usdt_ton_address_response(public_config),
            )
        if normalized == "/donate_ton":
            return _public_response(
                normalized,
                "public_donate_ton",
                format_public_donate_ton_address_response(public_config),
            )
        if normalized == "/donate_btc":
            return _public_response(
                normalized,
                "public_donate_btc",
                format_public_donate_btc_address_response(public_config),
            )
        if normalized == "/wolf":
            if _config_enabled(public_config, "wolf_briefing_enabled") and _config_enabled(
                public_config,
                "wolf_briefing_public_enabled",
            ):
                response = self.wolf_briefing_public_response(public_config=public_config, command="/wolf")
                return _public_response(
                    "/wolf",
                    "public_wolf_briefing",
                    response.text,
                    run_id=response.run_id,
                    public_config=public_config,
                )
            return _public_response(normalized, "public_admin_reserved", format_public_admin_reserved_response())
        if normalized == "/help":
            return _public_response(normalized, "public_help", format_public_help_response())
        if normalized in PUBLIC_ADMIN_RESERVED_COMMANDS:
            return _public_response(normalized, "public_admin_reserved", format_public_admin_reserved_response())
        return _public_response(
            normalized,
            "public_menu",
            format_public_menu_response(public_config),
            public_config=public_config,
        )

    def _latest_alerts_response(self) -> AdminCommandResponse:
        rows = self._latest_telegram_alert_rows()
        sent_count, blocked_count = self._telegram_alert_counts()
        lines: list[str] = [
            "Latest sent lifecycle alerts from local scan history.",
            "Manual execution only.",
            "",
            SCREEN_DIVIDER,
            f"Sent alerts: {_display(sent_count)}",
            f"Blocked attempts: {_display(blocked_count)}",
            "",
        ]
        if rows:
            lines.extend(_latest_alert_lines(rows, include_signal_id=True, max_rows=self._max_rows))
        else:
            lines.extend(("No sent lifecycle alerts found yet.", "The scanner has not published a public alert."))
        lines.extend(("", "No execution controls are available.", SCREEN_DIVIDER))
        return _admin_response("/latest", "latest", _screen("Latest Alerts", lines))

    def _wolf_briefing_response(self, *, admin_config: Any | None) -> AdminCommandResponse:
        if not _config_enabled(admin_config, "wolf_briefing_enabled"):
            text = _screen(
                "Wolf Briefing",
                (
                    "Wolf Briefing is disabled.",
                    "",
                    SCREEN_DIVIDER,
                    "Enable TELEGRAM_WOLF_BRIEFING_ENABLED=true to allow manual admin briefing delivery.",
                    "No scan, signal, or execution state was changed.",
                    SCREEN_DIVIDER,
                ),
            )
            return _admin_response(
                "/wolf",
                "wolf_briefing_disabled",
                text,
                admin_config=admin_config,
            )

        response = self.wolf_briefing_public_response(admin_config=admin_config, command="/wolf")
        return _admin_response(
            "/wolf",
            "wolf_briefing_preview",
            format_wolf_briefing_preview(response.text),
            run_id=response.run_id,
            admin_config=admin_config,
        )

    def wolf_briefing_public_response(
        self,
        *,
        admin_config: Any | None = None,
        public_config: Any | None = None,
        command: str = WOLF_BRIEFING_PUBLISH_COMMAND,
    ) -> AdminCommandResponse:
        artifacts = self._wolf_scan_artifacts()
        active_signals = load_active_public_signals(
            project_root=self._project_root,
            database_path=self._database_path,
            limit=self._max_rows,
        )
        active_watchlists = load_active_public_watchlists(
            project_root=self._project_root,
            database_path=self._database_path,
            limit=ACTIVE_WATCHLIST_DISPLAY_LIMIT,
        )
        snapshot = build_wolf_briefing_snapshot(
            manifest_row=artifacts.manifest_row,
            scan_payload=artifacts.scan_payload,
            active_signal_items=active_signals.items if active_signals.source_available else (),
            active_signal_count=active_signals.total if active_signals.source_available else None,
            watchlist_items=active_watchlists.items if active_watchlists.source_available else (),
            watchlist_count=active_watchlists.total if active_watchlists.source_available else None,
            max_focus=self._max_rows,
        )
        return AdminCommandResponse(
            command=command,
            response_type="wolf_briefing_public",
            text=format_wolf_briefing(snapshot, max_focus=self._max_rows),
            run_id=snapshot.run_id,
        )

    def _wolf_scan_artifacts(self) -> WolfScanArtifacts:
        db_artifacts = load_latest_db_scan_artifacts(
            project_root=self._project_root,
            database_path=self._database_path,
        )
        if db_artifacts.manifest_row is not None or db_artifacts.scan_payload is not None:
            return db_artifacts

        artifacts = self.latest_scan_artifacts()
        return WolfScanArtifacts(
            manifest_row=artifacts.manifest_row,
            scan_payload=artifacts.scan_payload,
            source_path=artifacts.scan_path,
        )

    def _public_status_response(self, *, public_config: Any | None) -> AdminCommandResponse:
        manifest_row = self.latest_manifest_row()
        lines: list[str] = [
            "Candle Craft public signal desk status.",
            "",
            SCREEN_DIVIDER,
            "Mode: Manual-only",
            "Execution: Disabled",
            "Quality gates: Protected",
            "Public UI: " + _enabled_disabled_na(public_config, "public_command_ui_enabled"),
            "Latest scan: " + (_display(manifest_row.get("timestamp")) if manifest_row is not None else NA),
            "Confirmed setups: " + (_display(manifest_row.get("valid_setup_count")) if manifest_row is not None else NA),
            "Watchlist setups: " + (_display(manifest_row.get("near_miss_count")) if manifest_row is not None else NA),
            SCREEN_DIVIDER,
            "",
            "The engine only posts filtered lifecycle alerts.",
        ]
        return _public_response("/status", "public_status", _screen("Status", lines))

    def _public_latest_alerts_response(self) -> AdminCommandResponse:
        rows = self._latest_telegram_alert_rows()
        lines: list[str] = [
            "Latest sent public lifecycle alerts.",
            "",
            SCREEN_DIVIDER,
        ]
        if rows:
            lines.extend(_latest_alert_lines(rows, include_signal_id=False, max_rows=self._max_rows))
        else:
            lines.extend(("No public lifecycle alerts have been sent yet.", "The engine is waiting for clean structure."))
        lines.extend(("", "Risk warning: crypto derivatives are high risk. Manual review only.", SCREEN_DIVIDER))
        return _public_response("/latest", "public_latest", _screen("Latest Alerts", lines))

    def _latest_telegram_alert_rows(self) -> tuple[Mapping[str, Any], ...]:
        connection = None
        try:
            connection = open_initialized_database(self._database_path)
            rows = connection.execute(
                """
                SELECT signal_id, symbol, direction, alert_type, sent_at
                FROM telegram_alert_attempts
                WHERE telegram_status = 'sent'
                ORDER BY id DESC
                LIMIT ?
                """,
                (self._max_rows,),
            ).fetchall()
        except (StorageError, sqlite3.Error, OSError):
            return ()
        finally:
            if connection is not None:
                connection.close()
        return tuple(dict(row) for row in rows)

    def _telegram_alert_counts(self) -> tuple[int, int]:
        connection = None
        try:
            connection = open_initialized_database(self._database_path)
            sent_count = connection.execute(
                "SELECT COUNT(*) FROM telegram_alert_attempts WHERE telegram_status = 'sent'"
            ).fetchone()[0]
            blocked_count = connection.execute(
                "SELECT COUNT(*) FROM telegram_alert_attempts WHERE telegram_status = 'blocked'"
            ).fetchone()[0]
        except (StorageError, sqlite3.Error, OSError):
            return 0, 0
        finally:
            if connection is not None:
                connection.close()
        return int(sent_count), int(blocked_count)

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
            text = _screen(
                "System Desk",
                (
                    "Candle Craft engine status.",
                    "Manual execution only.",
                    "",
                SCREEN_DIVIDER,
                "Mode: Manual-only",
                "Execution: Disabled",
                "Quality gates: Protected",
                "Duplicate protection: Active",
                "Safety audit: Active",
                "",
                "Latest scan:",
                "Run: N/A",
                "No recent scan summary available yet.",
                    SCREEN_DIVIDER,
                    "",
                    "The engine is filtering for quality.",
                    "No weak setups are promoted.",
                ),
            )
            return _admin_response("/status", "status", text, run_id=NA)

        text = _screen(
            "System Desk",
            (
                "Candle Craft engine status.",
                "Manual execution only.",
                "",
                SCREEN_DIVIDER,
                "Mode: Manual-only",
                "Execution: Disabled",
                "Quality gates: Protected",
                "Duplicate protection: Active",
                "Safety audit: Active",
                "",
                "Latest scan:",
                f"Run: {_run_text(manifest_row.get('run_id'))}",
                f"Symbol list: {_symbol_list_text(manifest_row)}",
                f"Market Climate: {_title_text(manifest_row.get('market_regime'))}",
                f"Regime confidence: {_display(manifest_row.get('regime_confidence'))}",
                f"Symbols scanned: {_display(manifest_row.get('symbols_scanned'))}",
                f"Confirmed setups: {_display(manifest_row.get('valid_setup_count'))}",
                f"Watch candidates: {_display(manifest_row.get('near_miss_count'))}",
                f"Alerts created: {_display(manifest_row.get('alerts_created'))}",
                SCREEN_DIVIDER,
                "",
                "The engine is filtering for quality.",
                "Weak setups stay rejected.",
            ),
        )
        return _admin_response("/status", "status", text, run_id=run_id)

    def _alerts_response(self) -> AdminCommandResponse:
        artifacts = self.latest_scan_artifacts()
        if artifacts.manifest_row is None:
            return _missing_scan_response(
                "/alerts",
                "alerts",
                f"No scan manifest rows found at {self._manifest_path}.",
                title="Alert Desk",
            )
        run_id = _display(artifacts.manifest_row.get("run_id"))
        if artifacts.scan_payload is None:
            return _missing_scan_response(
                "/alerts",
                "alerts",
                f"Latest scan file unavailable: {_display(artifacts.scan_path)}.",
                run_id=run_id,
                title="Alert Desk",
            )

        rows = _result_rows(artifacts.scan_payload)
        alert_rows = _alert_rows(rows)
        text = _screen(
            "Alert Desk",
            (
                "Latest lifecycle alerts from Candle Craft.",
                "Manual execution only.",
                "",
                SCREEN_DIVIDER,
                f"Run: {_run_text(artifacts.manifest_row.get('run_id'), artifacts.scan_payload.get('run_id'))}",
                f"Alerts created: {_first_text(artifacts.manifest_row.get('alerts_created'), len(alert_rows))}",
                f"Trade ideas detected: {_first_text(artifacts.manifest_row.get('trade_ideas_created'), _valid_count(rows))}",
                "",
                *(() if alert_rows else ("No lifecycle alerts available right now.",)),
                *(("Latest alert:", *_alert_lines(alert_rows, max_rows=self._max_rows)) if alert_rows else ()),
                "",
                "Risk note:",
                "Manual review only. No execution controls.",
                SCREEN_DIVIDER,
            ),
        )
        return _admin_response(
            "/alerts",
            "alerts",
            text,
            run_id=_first_text(artifacts.manifest_row.get("run_id"), artifacts.scan_payload.get("run_id")),
        )

    def _watchlists_response(self) -> AdminCommandResponse:
        text = self._watchlists_dashboard_text(include_lifecycle_fallback=True)
        return _admin_response(
            "/watchlists",
            "watchlists",
            text,
        )

    def _integrity_response(self, command: str) -> AdminCommandResponse:
        artifacts = self.latest_scan_artifacts()
        if artifacts.manifest_row is None:
            return _missing_scan_response(
                command,
                "integrity",
                f"No scan manifest rows found at {self._manifest_path}.",
                title="Integrity Desk",
            )
        run_id = _display(artifacts.manifest_row.get("run_id"))
        if artifacts.scan_payload is None:
            return _missing_scan_response(
                command,
                "integrity",
                f"Latest scan file unavailable: {_display(artifacts.scan_path)}.",
                run_id=run_id,
                title="Integrity Desk",
            )
        try:
            audit = audit_alert_integrity_artifact(
                artifacts.scan_payload,
                source=_display(artifacts.scan_path),
            )
        except Exception as exc:
            text = _screen(
                "Integrity Desk",
                (
                    "Alert safety and duplicate-control overview.",
                    "",
                    SCREEN_DIVIDER,
                    f"Run: {_run_text(run_id)}",
                    "No safety summary available yet.",
                    SCREEN_DIVIDER,
                ),
            )
            return _admin_response(command, "integrity", text, run_id=run_id)

        summary = audit.summary
        integrity_status = "Clean" if summary.is_valid else "Review required"
        text = _screen(
            "Integrity Desk",
            (
                "Alert safety and duplicate-control overview.",
                "",
                SCREEN_DIVIDER,
                f"Run: {_run_text(artifacts.manifest_row.get('run_id'), artifacts.scan_payload.get('run_id'))}",
                f"Safety status: {integrity_status}",
                f"Alerts reviewed: {summary.alert_count}",
                f"Duplicates blocked: {_first_text(artifacts.manifest_row.get('duplicate_alerts_blocked'), 0)}",
                f"Invalid alerts: {summary.invalid_alerts}",
                f"Missing safety data: {summary.missing_manifest_count}",
                "",
                "Findings:",
                *_integrity_issue_lines(audit.issues, max_rows=self._max_rows),
                "",
                "Rejected or incomplete setups are not alertable.",
                SCREEN_DIVIDER,
            ),
        )
        return _admin_response(
            command,
            "integrity",
            text,
            run_id=_first_text(artifacts.manifest_row.get("run_id"), artifacts.scan_payload.get("run_id")),
        )

    def _config_response(self, *, admin_config: Any | None) -> AdminCommandResponse:
        text = _screen(
            "Configuration Desk",
            (
                "Read-only system configuration.",
                "No settings can be changed from Telegram.",
                "",
                SCREEN_DIVIDER,
                "Manual mode: Active",
                "Execution: Disabled",
                f"Command UI: {_enabled_disabled_na(admin_config, 'command_ui_enabled')}",
                f"Admin reports: {_enabled_disabled_na(admin_config, 'admin_report_enabled')}",
                f"Wolf Briefing: {_enabled_disabled_na(admin_config, 'wolf_briefing_enabled')}",
                f"Public private Wolf Briefing: {_enabled_disabled_na(admin_config, 'wolf_briefing_public_enabled')}",
                (
                    "Wolf channel publish: "
                    f"{_enabled_disabled_na(admin_config, 'wolf_briefing_channel_publish_enabled')}"
                ),
                f"Test mode: {_active_inactive_na(admin_config, 'dry_run')}",
                "Quality gates: Protected",
                "Signal filters: Protected",
                "",
                "Sensitive data:",
                f"Bot token: {_hidden_status(admin_config, 'bot_token')}",
                f"Chat ID: {_hidden_status(admin_config, 'admin_chat_id')}",
                f"Wolf publish channel: {_config_presence(admin_config, 'wolf_briefing_publish_channel_id')}",
                f"Signal channel invite: {_config_presence(admin_config, 'signal_channel_invite_link')}",
                SCREEN_DIVIDER,
                "",
                "This panel is informational only.",
            ),
        )
        return _admin_response("/config", "config", text)

    def _public_lastscan_response(self) -> AdminCommandResponse:
        artifacts = self.latest_scan_artifacts()
        if artifacts.manifest_row is None:
            text = _screen(
                "Last Scan",
                (
                    "Latest Candle Craft market intelligence.",
                    "",
                    SCREEN_DIVIDER,
                    "Last scan: N/A",
                    "Symbols scanned: N/A",
                    "Confirmed setups: N/A",
                    "Watchlist setups: N/A",
                    "Market Climate: N/A",
                    SCREEN_DIVIDER,
                    "",
                    "The engine only promotes setups that pass the filters.",
                ),
            )
            return _public_response("/lastscan", "public_lastscan", text)

        text = _screen(
            "Last Scan",
            (
                "Latest Candle Craft market intelligence.",
                "",
                SCREEN_DIVIDER,
                f"Last scan: {_display(artifacts.manifest_row.get('timestamp'))}",
                f"Symbols scanned: {_display(artifacts.manifest_row.get('symbols_scanned'))}",
                f"Confirmed setups: {_display(artifacts.manifest_row.get('valid_setup_count'))}",
                f"Watchlist setups: {_display(artifacts.manifest_row.get('near_miss_count'))}",
                f"Market Climate: {_title_text(artifacts.manifest_row.get('market_regime'))}",
                SCREEN_DIVIDER,
                "",
                "The engine only promotes setups that pass the filters.",
            ),
        )
        return _public_response(
            "/lastscan",
            "public_lastscan",
            text,
            run_id=_display(artifacts.manifest_row.get("run_id")),
        )

    def _public_signals_response(self) -> AdminCommandResponse:
        result = load_active_public_signals(
            project_root=self._project_root,
            database_path=self._database_path,
            limit=self._max_rows,
        )
        lines: list[str] = [
            "Current active signal records.",
            "Select a symbol for details.",
            "",
            SCREEN_DIVIDER,
        ]
        if not result.source_available or result.total == 0:
            lines.extend(
                (
                    "No active confirmed signals right now.",
                    "",
                    "The engine is waiting for clean structure.",
                )
            )
        else:
            lines.append(f"Active signals: {result.total}")
            if result.total > len(result.items):
                lines.append(f"Showing {len(result.items)}.")
        lines.append(SCREEN_DIVIDER)
        return _public_response(
            "/signals",
            "public_signals",
            _screen("Active Signals", lines),
            reply_markup=public_active_signals_inline_markup(result.items) if result.items else None,
        )

    def _signal_detail_response(
        self,
        command: str,
        *,
        public: bool,
        admin_config: Any | None = None,
        public_config: Any | None = None,
    ) -> AdminCommandResponse:
        normalized = normalize_admin_command(command)
        selector = _signal_detail_selector(command)
        result = load_active_signal_detail(
            project_root=self._project_root,
            database_path=self._database_path,
            selector=selector,
        )
        detail = result.detail
        if detail is None:
            text = _signal_detail_missing_text(selector, source_available=result.source_available)
            symbol = selector
        else:
            symbol = _display(detail.symbol)
            if normalized == "/signal_lifecycle":
                text = format_signal_detail_lifecycle(detail)
            elif normalized == "/signal_why":
                text = format_signal_detail_why_valid(detail)
            else:
                text = format_signal_detail(detail)
        if public:
            return _public_response(
                command,
                f"public_signal_detail{_signal_detail_response_suffix(normalized)}",
                text,
                reply_markup=signal_detail_inline_markup(symbol, scope="public"),
                public_config=public_config,
            )
        return _admin_response(
            command,
            f"signal_detail{_signal_detail_response_suffix(normalized)}",
            text,
            reply_markup=signal_detail_inline_markup(symbol, scope="admin"),
            admin_config=admin_config,
        )

    def _public_watchlist_response(self, *, command: str = "/watchlists") -> AdminCommandResponse:
        return _public_response(
            command,
            "public_watchlist",
            self._watchlists_dashboard_text(include_lifecycle_fallback=True),
        )

    def _watchlists_dashboard_text(self, *, include_lifecycle_fallback: bool) -> str:
        result = load_watchlist_stage_dashboard(
            project_root=self._project_root,
            database_path=self._database_path,
            limit=WATCHLIST_STAGE_DISPLAY_LIMIT,
            include_lifecycle_fallback=include_lifecycle_fallback,
        )
        return format_watchlist_stage_dashboard(result)

    def _lastscan_response(self) -> AdminCommandResponse:
        artifacts = self.latest_scan_artifacts()
        if artifacts.manifest_row is None:
            return _missing_scan_response(
                "/lastscan",
                "lastscan",
                f"No scan manifest rows found at {self._manifest_path}.",
                title="Latest Scan",
            )
        if artifacts.scan_payload is None:
            return _missing_scan_response(
                "/lastscan",
                "lastscan",
                f"Latest scan file unavailable: {_display(artifacts.scan_path)}.",
                run_id=_display(artifacts.manifest_row.get("run_id")),
                title="Latest Scan",
            )

        payload = artifacts.scan_payload
        manifest_row = artifacts.manifest_row
        rows = _result_rows(payload)
        valid_rows = _valid_rows(rows)
        near_rows = _near_rows(rows)
        summary_rows = valid_rows if valid_rows else near_rows
        section_title = "Top Confirmed Setups" if valid_rows else "Top Watch Candidates"
        lines = (
            "Latest scan summary.",
            "Manual execution only.",
            "",
            SCREEN_DIVIDER,
            f"Run: {_run_text(manifest_row.get('run_id'), payload.get('run_id'))}",
            f"Timestamp: {_display(manifest_row.get('timestamp'))}",
            f"Symbol list: {_symbol_list_text(manifest_row, payload)}",
            f"Market Climate: {_title_text(_first_text(manifest_row.get('market_regime'), _nested_value(payload, 'market_regime', 'state')))}",
            f"Symbols scanned: {_first_text(manifest_row.get('symbols_scanned'), payload.get('scanned_symbols'), len(rows))}",
            f"Confirmed setups: {_first_text(manifest_row.get('valid_setup_count'), len(valid_rows))}",
            f"Watch candidates: {_first_text(manifest_row.get('near_miss_count'), len(near_rows))}",
            f"Rejected: {_first_text(manifest_row.get('rejected_count'), _rejected_count(rows))}",
            "",
            section_title,
            *_setup_lines(summary_rows, max_rows=self._max_rows),
            SCREEN_DIVIDER,
            "",
            "No setup is promoted without confirmation.",
        )
        return _admin_response(
            "/lastscan",
            "lastscan",
            _screen("Latest Scan", lines),
            run_id=_first_text(manifest_row.get("run_id"), payload.get("run_id")),
        )

    def _near_response(self) -> AdminCommandResponse:
        artifacts = self.latest_scan_artifacts()
        if artifacts.manifest_row is None:
            return _missing_scan_response(
                "/near",
                "near",
                f"No scan manifest rows found at {self._manifest_path}.",
                title="Watch Candidates",
            )
        if artifacts.scan_payload is None:
            return _missing_scan_response(
                "/near",
                "near",
                f"Latest scan file unavailable: {_display(artifacts.scan_path)}.",
                run_id=_display(artifacts.manifest_row.get("run_id")),
                title="Watch Candidates",
            )
        rows = _near_rows(_result_rows(artifacts.scan_payload))
        lines = (
            "Watch candidates are monitored, not promoted.",
            "Manual execution only.",
            "",
            SCREEN_DIVIDER,
            f"Run: {_run_text(artifacts.manifest_row.get('run_id'), artifacts.scan_payload.get('run_id'))}",
            "",
            *_near_lines(rows, max_rows=self._max_rows),
            SCREEN_DIVIDER,
            "",
            "The engine is filtering.",
            "No forced trades.",
        )
        return _admin_response(
            "/near",
            "near",
            _screen("Watch Candidates", lines),
            run_id=_first_text(artifacts.manifest_row.get("run_id"), artifacts.scan_payload.get("run_id")),
        )

    def _blocked_response(self) -> AdminCommandResponse:
        artifacts = self.latest_scan_artifacts()
        if artifacts.manifest_row is None:
            return _missing_scan_response(
                "/blocked",
                "blocked",
                f"No scan manifest rows found at {self._manifest_path}.",
                title="Integrity Desk",
            )
        if artifacts.scan_payload is None:
            return _missing_scan_response(
                "/blocked",
                "blocked",
                f"Latest scan file unavailable: {_display(artifacts.scan_path)}.",
                run_id=_display(artifacts.manifest_row.get("run_id")),
                title="Integrity Desk",
            )
        rows = _blocked_rows(_result_rows(artifacts.scan_payload))
        if not rows:
            text = _screen(
                "Integrity Desk",
                (
                    "Safety checks for skipped alerts.",
                    "",
                    SCREEN_DIVIDER,
                    f"Run: {_run_text(artifacts.manifest_row.get('run_id'), artifacts.scan_payload.get('run_id'))}",
                    "No safety blocks in the latest scan.",
                    SCREEN_DIVIDER,
                ),
            )
        else:
            text = _screen(
                "Integrity Desk",
                (
                    "Safety checks for skipped alerts.",
                    "",
                    SCREEN_DIVIDER,
                    f"Run: {_run_text(artifacts.manifest_row.get('run_id'), artifacts.scan_payload.get('run_id'))}",
                    "",
                    *_blocked_lines(rows, max_rows=self._max_rows),
                    SCREEN_DIVIDER,
                ),
            )
        return _admin_response(
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
    return _screen(
        "Candle Craft Intelligence",
        (
            "Your market-structure command center.",
            "Manual execution. Quality gates protected.",
            "",
            SCREEN_DIVIDER,
            "🐺 Wolf Briefing",
            "Current market mood, active signals, watchlist, and near-miss focus.",
            "",
            "📊 System Desk",
            "Engine health, scan status, and safety mode.",
            "",
            "🚨 Alert Desk",
            "Latest lifecycle alerts from the intelligence engine.",
            "",
            "👁 Watchlist Desk",
            "Setups being monitored or waiting for confirmation.",
            "",
            "🧾 Integrity Desk",
            "Duplicate control, skipped alerts, and safety checks.",
            "",
            "⚙️ Configuration",
            "Read-only bot and alert settings.",
            "",
            "❓ Command Guide",
            "Available commands and safety rules.",
            SCREEN_DIVIDER,
            "",
            "No execution buttons.",
            "No forced trades.",
            "Quality over quantity.",
        ),
    )


def format_menu_response() -> str:
    return format_start_response()


def format_about_response() -> str:
    return _screen(
        "About",
        (
            "Candle Craft Intelligence monitors crypto futures structure.",
            "",
            SCREEN_DIVIDER,
            "Mode: Manual-only",
            "Execution: Disabled",
            "Order placement: Not available",
            "Withdrawals/transfers: Not available",
            "Quality gates: Protected",
            SCREEN_DIVIDER,
            "",
            "Alerts are trade ideas for manual review, not financial advice.",
        ),
    )


def format_public_about_response(config: Any | None = None) -> str:
    return _screen(
        "About Candle Craft",
        (
            "Candle Craft Intelligence filters crypto futures for clean structure.",
            _public_signal_channel_copy(config),
            "",
            SCREEN_DIVIDER,
            "Public alerts are lifecycle updates from the signal engine.",
            "No execution controls are available from Telegram.",
            "No financial advice.",
            "Risk management is always your responsibility.",
            SCREEN_DIVIDER,
            "",
            "Quality over quantity.",
        ),
    )


def format_public_menu_response(config: Any | None = None) -> str:
    return _screen(
        "Candle Craft Intelligence",
        (
            "Your AI-powered signal engine is online.",
            "",
            "Welcome to the Moon Trip signal desk.",
            "",
            (
                "Candle Craft filters crypto futures for clean structure, liquidity sweeps, confirmations, "
                "and high-quality setups."
            ),
            "",
            "No random signals.",
            "No market chasing.",
            "Only filtered opportunities when the structure is clean.",
            "",
            SCREEN_DIVIDER,
            "Use the buttons below to access the signal channel and bot info.",
            _public_signal_channel_copy(config),
            "",
            "System:",
            "Manual signal intelligence only. No order execution.",
        ),
    )


def format_public_admin_reserved_response() -> str:
    return _screen(
        "Candle Craft Intelligence",
        (
            "That signal desk view is not available here.",
            "",
            "Use the buttons below to enter the signal desk.",
        ),
    )


def format_public_social_response(config: Any | None) -> str:
    return _screen(
        "Social",
        (
            "Official Candle Craft links.",
            "",
            SCREEN_DIVIDER,
            "X / Twitter:",
            _public_url(config, "x_url", fallback=NA),
            "",
            "Telegram:",
            _public_url(config, "telegram_url", fallback=NA),
            SCREEN_DIVIDER,
            "",
            "Only trust official Candle Craft links.",
        ),
    )


def format_public_donate_response(config: Any | None) -> str:
    return _screen(
        "Donate",
        (
            "Thank you for supporting the Candle Craft engine.",
            "",
            (
                "Your support helps us keep improving the signal desk, research tools, "
                "and market-structure intelligence behind Candle Craft."
            ),
            "",
            SCREEN_DIVIDER,
            "How to donate:",
            "",
            "1. Choose your preferred cryptocurrency below.",
            "2. Tap the button to copy the address.",
            "3. Open your wallet and send your donation.",
            "",
            "We appreciate every bit of support from the Candle Craft community.",
            SCREEN_DIVIDER,
            "",
            "Always verify the network before sending.",
            "Support is optional.",
        ),
    )


def format_public_donate_usdt_ton_address_response(config: Any | None) -> str:
    return _format_public_donate_address_response(
        config,
        title="USDT on TON",
        address_name="donate_usdt_ton_address",
        network="TON",
        send_warning="Send only USDT on TON to this address.",
    )


def format_public_donate_ton_address_response(config: Any | None) -> str:
    return _format_public_donate_address_response(
        config,
        title="TON",
        address_name="donate_ton_address",
        network="TON",
        send_warning="Send only TON to this address.",
    )


def format_public_donate_btc_address_response(config: Any | None) -> str:
    return _format_public_donate_address_response(
        config,
        title="BTC",
        address_name="donate_btc_address",
        network="Bitcoin",
        send_warning="Send only BTC to this address.",
    )


def format_public_help_response() -> str:
    return _screen(
        "Help",
        (
            "How to use the Candle Craft signal desk.",
            "",
            SCREEN_DIVIDER,
            "📡 Last Scan",
            "Latest market intelligence.",
            "",
            "🔥 Active Signals",
            "Confirmed setups only.",
            "",
            "👁 Watchlists",
            "Active public watchlist plans.",
            "",
            "🌐 Social",
            "Official Candle Craft links.",
            "",
            "🧡 Donate",
            "Optional support for development.",
            SCREEN_DIVIDER,
            "",
            "Important:",
            "No financial advice.",
            "No guaranteed outcomes.",
            "Risk management is always your responsibility.",
        ),
    )


def format_help_response() -> str:
    return _screen(
        "Command Guide",
        (
            "Available Candle Craft commands.",
            "",
            SCREEN_DIVIDER,
            "/menu",
            "Open the main command center.",
            "",
            "/status",
            "View system health and scan state.",
            "",
            "/wolf",
            "View the latest Wolf Briefing.",
            "",
            "/alerts",
            "View latest lifecycle alerts.",
            "",
            "/watchlists",
            "View active monitored setups.",
            "",
            "/audit",
            "View safety and duplicate checks.",
            "",
            "/config",
            "View read-only configuration.",
            "",
            "/help",
            "Show this guide.",
            SCREEN_DIVIDER,
            "",
            "Safety rules:",
            "No execution.",
            "No forced trades.",
            "No weak setup promotion.",
            "Manual review only.",
        ),
    )


def format_wolf_briefing_preview(public_text: str) -> str:
    lines = str(public_text or "").splitlines()
    if lines and lines[0].strip() == "🐺🟠 WOLF BRIEFING":
        lines = lines[1:]
        if lines and not lines[0].strip():
            lines = lines[1:]
    body = "\n".join(lines).strip() or NA
    return f"{WOLF_BRIEFING_PREVIEW_HEADER}\n\n{body}"


def _signal_detail_selector(command: str | None) -> str:
    parts = str(command or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return NA
    selector = parts[1].strip()
    return selector.upper() if selector else NA


def _signal_detail_response_suffix(command: str) -> str:
    if command == "/signal_lifecycle":
        return "_lifecycle"
    if command == "/signal_why":
        return "_why"
    return ""


def _signal_detail_missing_text(selector: str, *, source_available: bool) -> str:
    symbol = _display(selector)
    if source_available:
        status_lines = (
            "Status: This setup is no longer active.",
            "It may have been closed, invalidated, or expired.",
        )
    else:
        status_lines = (
            "Status: No local active signal source found.",
            "The scanner has not published an active signal record yet.",
        )
    return "\n".join(
        (
            f"{SIGNAL_HEADER_PREFIX} {symbol} \u2014 SIGNAL DETAIL",
            "",
            *status_lines,
            "",
            SIGNAL_FOOTER,
        )
    )


def normalize_admin_command(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    mapped = SIMPLE_REPLY_BUTTON_COMMANDS.get(text.lower())
    if mapped is not None:
        return mapped
    mapped = ADMIN_MENU_BUTTON_COMMANDS.get(text.lower())
    if mapped is not None:
        return mapped
    mapped = PUBLIC_MENU_BUTTON_COMMANDS.get(text.lower())
    if mapped is not None:
        return mapped
    token = text.split()[0].split("@", 1)[0].lower()
    return token


def command_for_callback_data(value: str | None) -> tuple[str, str]:
    text = str(value or "").strip().lower()
    dynamic_scope, dynamic_command = _signal_detail_command_for_callback(text)
    if dynamic_scope:
        return dynamic_scope, dynamic_command
    public_command = PUBLIC_CALLBACK_COMMANDS.get(text)
    if public_command is not None:
        return "public", public_command
    wolf_command = ADMIN_WOLF_BRIEFING_CALLBACK_COMMANDS.get(text)
    if wolf_command is not None:
        return "admin", wolf_command
    admin_command = ADMIN_CALLBACK_COMMANDS.get(text)
    if admin_command is not None:
        return "admin", admin_command
    return "", ""


def _signal_detail_command_for_callback(text: str) -> tuple[str, str]:
    for scope in ("public", "admin"):
        for action, command in (
            ("signal_lifecycle", "/signal_lifecycle"),
            ("signal_why", "/signal_why"),
            ("signal", "/signal"),
        ):
            prefix = f"{scope}:{action}:"
            if text.startswith(prefix):
                selector = text[len(prefix) :].strip()
                if selector:
                    return scope, f"{command} {selector}"
    return "", ""


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


def admin_menu_inline_markup() -> Mapping[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": ADMIN_MENU_BUTTON_CALLBACKS[label]} for label in row]
            for row in ADMIN_MENU_BUTTON_ROWS
        ],
    }


def public_menu_inline_markup(config: Any | None = None) -> Mapping[str, Any]:
    keyboard = _signal_channel_button_rows(config)
    keyboard.extend(
        [
            [{"text": label, "callback_data": PUBLIC_MENU_BUTTON_CALLBACKS[label]} for label in row]
            for row in PUBLIC_MENU_BUTTON_ROWS
        ]
    )
    return {"inline_keyboard": keyboard}


def admin_back_to_menu_inline_markup() -> Mapping[str, Any]:
    return {"inline_keyboard": [[{"text": "↩ Back to Menu", "callback_data": "admin:menu"}]]}


def wolf_briefing_preview_inline_markup() -> Mapping[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": WOLF_BRIEFING_PUBLISH_BUTTON_LABEL, "callback_data": "admin:wolf_publish"}],
            [
                {"text": WOLF_BRIEFING_REFRESH_BUTTON_LABEL, "callback_data": "admin:wolf_refresh"},
                {"text": WOLF_BRIEFING_CANCEL_BUTTON_LABEL, "callback_data": "admin:wolf_cancel"},
            ],
        ]
    }


def admin_watchlists_inline_markup(config: Any | None = None) -> Mapping[str, Any]:
    keyboard = _signal_channel_button_rows(config)
    keyboard.extend(
        [
            [{"text": WATCHLIST_REFRESH_BUTTON_LABEL, "callback_data": "admin:watchlists"}],
            [{"text": WATCHLIST_BACK_BUTTON_LABEL, "callback_data": "admin:menu"}],
        ]
    )
    return {"inline_keyboard": keyboard}


def public_watchlists_inline_markup(config: Any | None = None) -> Mapping[str, Any]:
    keyboard = _signal_channel_button_rows(config)
    keyboard.extend(
        [
            [{"text": WATCHLIST_REFRESH_BUTTON_LABEL, "callback_data": "public:watchlist"}],
            [{"text": WATCHLIST_BACK_BUTTON_LABEL, "callback_data": "public:menu"}],
        ]
    )
    return {"inline_keyboard": keyboard}


def public_active_signals_inline_markup(items: Sequence[ActiveSignalItem]) -> Mapping[str, Any]:
    return {"inline_keyboard": _signal_button_rows(items, scope="public")}


def signal_detail_inline_markup(symbol: str, *, scope: str) -> Mapping[str, Any]:
    safe_scope = "admin" if scope == "admin" else "public"
    selector = _callback_selector(symbol)
    back_callback = "admin:menu" if safe_scope == "admin" else "public:signals"
    return {
        "inline_keyboard": [
            [{"text": SIGNAL_DETAIL_REFRESH_BUTTON_LABEL, "callback_data": f"{safe_scope}:signal:{selector}"}],
            [
                {
                    "text": SIGNAL_DETAIL_LIFECYCLE_BUTTON_LABEL,
                    "callback_data": f"{safe_scope}:signal_lifecycle:{selector}",
                },
                {
                    "text": SIGNAL_DETAIL_WHY_VALID_BUTTON_LABEL,
                    "callback_data": f"{safe_scope}:signal_why:{selector}",
                },
            ],
            [{"text": SIGNAL_DETAIL_BACK_BUTTON_LABEL, "callback_data": back_callback}],
        ]
    }


def public_back_to_menu_inline_markup(config: Any | None = None) -> Mapping[str, Any]:
    keyboard = _signal_channel_button_rows(config)
    keyboard.append([{"text": "↩ Back to Menu", "callback_data": "public:menu"}])
    return {"inline_keyboard": keyboard}


def public_donate_inline_markup(config: Any | None = None) -> Mapping[str, Any]:
    keyboard: list[list[dict[str, Any]]] = []
    for label, address_name in (
        ("📋 USDT on TON", "donate_usdt_ton_address"),
        ("📋 TON", "donate_ton_address"),
        ("📋 BTC", "donate_btc_address"),
    ):
        address = _public_config_text(config, address_name, fallback=NA)
        if address != NA:
            keyboard.append([{"text": label, "copy_text": {"text": address}}])
    keyboard.append([{"text": "⬅️ Back to Menu", "callback_data": "public:menu"}])
    return {"inline_keyboard": keyboard}


def reply_keyboard_remove_markup() -> Mapping[str, Any]:
    return {"remove_keyboard": True}


def admin_menu_reply_markup() -> Mapping[str, Any]:
    return admin_menu_inline_markup()


def public_menu_reply_markup() -> Mapping[str, Any]:
    return public_menu_inline_markup()


def _admin_response(
    command: str,
    response_type: str,
    text: str,
    *,
    run_id: str = NA,
    reply_markup: Mapping[str, Any] | None = None,
    admin_config: Any | None = None,
) -> AdminCommandResponse:
    return AdminCommandResponse(
        command=command,
        response_type=response_type,
        text=text,
        run_id=run_id,
        reply_markup=reply_markup or _admin_reply_markup_for(command, response_type, admin_config),
        cleanup_reply_keyboard=command in {"/start", "/menu"},
    )


def _public_response(
    command: str,
    response_type: str,
    text: str,
    *,
    run_id: str = NA,
    photo_path: Path | None = None,
    photo_url: str | None = None,
    reply_markup: Mapping[str, Any] | None = None,
    suppress_reply_markup: bool = False,
    public_config: Any | None = None,
) -> AdminCommandResponse:
    return AdminCommandResponse(
        command=command,
        response_type=response_type,
        text=text,
        run_id=run_id,
        reply_markup=(
            None
            if suppress_reply_markup
            else (reply_markup or _public_reply_markup_for(command, response_type, public_config))
        ),
        photo_path=photo_path,
        photo_url=photo_url,
        cleanup_reply_keyboard=command in {"/start", "/menu"},
    )


def _admin_reply_markup_for(command: str, response_type: str, config: Any | None = None) -> Mapping[str, Any]:
    if response_type == "wolf_briefing_preview":
        return wolf_briefing_preview_inline_markup()
    if response_type == "watchlists":
        return admin_watchlists_inline_markup(config)
    signal_rows = _signal_channel_button_rows(config)
    if command in {"/start", "/menu"} or response_type in {"start", "menu"}:
        markup = admin_menu_inline_markup()
        return {"inline_keyboard": signal_rows + list(markup["inline_keyboard"])}
    markup = admin_back_to_menu_inline_markup()
    return {"inline_keyboard": signal_rows + list(markup["inline_keyboard"])}


def _public_reply_markup_for(command: str, response_type: str, config: Any | None = None) -> Mapping[str, Any]:
    if command in {"/start", "/menu"} or response_type == "public_menu":
        return public_menu_inline_markup(config)
    if response_type == "public_watchlist":
        return public_watchlists_inline_markup(config)
    return public_back_to_menu_inline_markup(config)


def _signal_channel_button_rows(config: Any | None) -> list[list[dict[str, Any]]]:
    invite_link = _signal_channel_invite_link(config)
    if invite_link == NA:
        return []
    return [[{"text": JOIN_SIGNAL_CHANNEL_BUTTON_LABEL, "url": invite_link}]]


def _signal_button_rows(items: Sequence[ActiveSignalItem], *, scope: str) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    current_row: list[dict[str, Any]] = []
    safe_scope = "admin" if scope == "admin" else "public"
    for item in items:
        symbol = _display(item.symbol)
        if symbol == NA:
            continue
        current_row.append({"text": symbol, "callback_data": f"{safe_scope}:signal:{_callback_selector(symbol)}"})
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    return rows


def _callback_selector(value: Any) -> str:
    text = _display(value)
    return "na" if text == NA else text.strip().upper()


def _public_signal_channel_copy(config: Any | None) -> str:
    return PUBLIC_SIGNAL_CHANNEL_COPY if _signal_channel_invite_link(config) != NA else PUBLIC_SIGNAL_CHANNEL_MISSING_COPY


def _signal_channel_invite_link(config: Any | None) -> str:
    if config is None:
        return NA
    return _display(getattr(config, "signal_channel_invite_link", NA))


def _screen(title: str, lines: Sequence[str]) -> str:
    body = [f"{SCREEN_HEADER} {title}", ""]
    body.extend(lines)
    body.extend(("", SCREEN_FOOTER))
    return "\n".join(body)


def _missing_scan_response(
    command: str,
    response_type: str,
    reason: str,
    *,
    run_id: str = NA,
    title: str = "Latest Scan",
) -> AdminCommandResponse:
    return _admin_response(
        command,
        response_type,
        _screen(
            title,
            (
                "Manual execution only.",
                "",
                SCREEN_DIVIDER,
                f"Run: {_run_text(run_id)}",
                _empty_state_text(response_type),
                SCREEN_DIVIDER,
            ),
        ),
        run_id=run_id,
    )


def _command_list_text() -> str:
    return "Available commands: " + ", ".join(ADMIN_COMMANDS)


def _empty_state_text(response_type: str) -> str:
    if response_type == "alerts":
        return "No lifecycle alerts available right now."
    if response_type in {"watchlists", "near"}:
        return "No active watchlist setups right now."
    if response_type in {"integrity", "blocked"}:
        return "No safety summary available yet."
    return "No recent scan summary available yet."


def _run_text(*values: Any) -> str:
    return _short_run_id(_first_text(*values))


def _short_run_id(value: Any, max_length: int = 12) -> str:
    text = _display(value)
    if text == NA or len(text) <= max_length:
        return text
    return text[:max_length]


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
    lines: list[str] = []
    for row in rows[:max_rows]:
        if lines:
            lines.append("")
        lines.extend(
            (
                _display(row.get("symbol")),
                f"Direction: {_title_text(_row_side(row))}",
                f"Grade: {_row_grade(row)}",
                f"Score: {_row_score(row)}",
                f"Status: {_setup_status_text(row)}",
                f"Reason: {_row_short_reason(row)}",
                f"Next step: {_next_step_text(_row_next_trigger(row))}",
            )
        )
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _near_lines(rows: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    if not rows:
        return ["No watch candidates in the latest scan."]
    lines: list[str] = []
    for row in rows[:max_rows]:
        if lines:
            lines.append("")
        lines.extend(
            (
                _display(row.get("symbol")),
                f"Direction: {_title_text(_row_side(row))}",
                f"Grade: {_row_grade(row)}",
                f"Score: {_row_score(row)}",
                "Status: Monitoring",
                f"Next step: {_next_step_text(_row_next_trigger(row))}",
            )
        )
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _blocked_lines(rows: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    lines: list[str] = []
    for row in rows[:max_rows]:
        if lines:
            lines.append("")
        lines.extend(
            (
                _display(row.get("symbol")),
                f"Direction: {_title_text(_row_side(row))}",
                f"Safety check: {_safety_reason_text(row)}",
                f"Next step: {_next_step_text(_row_next_trigger(row))}",
                f"Status: {_lifecycle_text(row)}",
            )
        )
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _alert_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(row for row in rows if _has_alert_record(row))


def _has_alert_record(row: Mapping[str, Any]) -> bool:
    alert_result = row.get("alert_result")
    if isinstance(alert_result, Mapping):
        return True
    history = _status_history(row.get("status_history"))
    return "alert_dry_run_created" in history or _display(row.get("status")) == "alert_dry_run_created"


def _alert_lines(rows: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    if not rows:
        return ["None"]
    lines: list[str] = []
    for row in rows[:max_rows]:
        if lines:
            lines.append("")
        lines.extend(
            (
                f"Symbol: {_display(row.get('symbol'))}",
                f"Direction: {_title_text(_row_side(row))}",
                f"Grade: {_row_grade(row)}",
                f"Score: {_row_score(row)}",
                f"Delivery: {_delivery_text(_alert_delivery_status(row))}",
                f"Safety check: {_integrity_text(_alert_integrity_status(row))}",
            )
        )
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _public_artifact_rows(artifacts: LatestScanArtifacts) -> tuple[Mapping[str, Any], ...]:
    if artifacts.scan_payload is None:
        return ()
    return _result_rows(artifacts.scan_payload)


def _public_run_id(artifacts: LatestScanArtifacts) -> str:
    if artifacts.manifest_row is None:
        return NA
    if artifacts.scan_payload is None:
        return _display(artifacts.manifest_row.get("run_id"))
    return _first_text(artifacts.manifest_row.get("run_id"), artifacts.scan_payload.get("run_id"))


def _public_active_signal_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(
        row
        for row in rows
        if not _is_target_integrity_blocked(row)
        and (_display_status(row) == "valid_setup" or _display(row.get("display_bucket")) == "valid")
    )


def _public_watchlist_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _near_rows(rows)


def _public_signal_lines(rows: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    lines: list[str] = []
    for row in rows[:max_rows]:
        if lines:
            lines.append("")
        lines.extend(
            (
                f"Symbol: {_display(row.get('symbol'))}",
                f"Direction: {_title_text(_row_side(row))}",
                f"Grade: {_row_grade(row)}",
                f"Entry: {_entry_text(row)}",
                f"Stop: {_stop_text(row)}",
                f"Targets: {_targets_text(row)}",
                f"Status: {_setup_status_text(row)}",
                f"Updated: {_updated_text(row)}",
            )
        )
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _public_watchlist_lines(rows: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    lines: list[str] = []
    for row in rows[:max_rows]:
        if lines:
            lines.append("")
        lines.extend(
            (
                f"Symbol: {_display(row.get('symbol'))}",
                f"Direction: {_title_text(_row_side(row))}",
                f"Grade: {_row_grade(row)}",
                "Status: Monitoring",
                f"Waiting for: {_watch_waiting_for_text(row)}",
                f"Invalidation: {_invalidation_text(row)}",
            )
        )
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _watchlist_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(
        row
        for row in rows
        if not _is_target_integrity_blocked(row)
        and (
            _display_status(row) == "near_miss"
            or _display(row.get("display_bucket")) == "near_miss"
            or _display(row.get("lifecycle_current_state")).upper() in {"WATCHLISTED", "STALKING", "TRIGGERED"}
        )
    )


def _watchlist_lines(rows: Sequence[Mapping[str, Any]], *, max_rows: int) -> list[str]:
    if not rows:
        return ["None"]
    lines: list[str] = []
    for row in rows[:max_rows]:
        if lines:
            lines.append("")
        lines.extend(
            (
                f"Symbol: {_display(row.get('symbol'))}",
                f"Direction: {_title_text(_row_side(row))}",
                f"Grade: {_row_grade(row)}",
                f"Score: {_row_score(row)}",
                "Status: Monitoring",
                f"Next step: {_next_step_text(_row_next_trigger(row))}",
            )
        )
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _watchlist_preset_lines() -> list[str]:
    return [f"{_preset_name_text(name)}: {count} symbols" for name, count in presets_with_counts()]


def _integrity_issue_lines(issues: Sequence[Any], *, max_rows: int) -> list[str]:
    visible = [
        issue
        for issue in issues
        if _display(getattr(issue, "severity", NA)) in {"warning", "blocker", "error"}
    ]
    if not visible:
        return ["None"]
    lines = [
        f"{_title_text(getattr(issue, 'severity', NA))}: {_finding_text(getattr(issue, 'code', NA))}"
        for issue in visible[:max_rows]
    ]
    return _with_omitted(lines, total=len(visible), max_rows=max_rows)


def _latest_alert_lines(
    rows: Sequence[Mapping[str, Any]],
    *,
    include_signal_id: bool,
    max_rows: int,
) -> list[str]:
    visible = tuple(rows[:max_rows])
    lines: list[str] = []
    for row in visible:
        symbol = _display(row.get("symbol"))
        direction = _title_text(row.get("direction"))
        alert_type = _alert_type_label(row.get("alert_type"))
        sent_at = _display(row.get("sent_at"))
        first_line = f"{symbol} {direction} - {alert_type}" if direction != NA else f"{symbol} - {alert_type}"
        lines.append(first_line)
        lines.append(f"Sent: {sent_at}")
        if include_signal_id:
            lines.append(f"Signal: {_short_signal_id(row.get('signal_id'))}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return _with_omitted(lines, total=len(rows), max_rows=max_rows)


def _alert_type_label(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    return _title_text(text.replace("_", " "))


def _short_signal_id(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    return text if len(text) <= 12 else f"{text[:8]}..."


def _with_omitted(lines: list[str], *, total: int, max_rows: int) -> list[str]:
    if total > max_rows:
        lines.append(f"{total - max_rows} more not shown.")
    return lines


def _valid_count(rows: Sequence[Mapping[str, Any]]) -> int:
    return len(_valid_rows(rows))


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


def _alert_delivery_status(row: Mapping[str, Any]) -> str:
    alert = row.get("alert_result")
    if isinstance(alert, Mapping):
        return _first_text(alert.get("status"), alert.get("delivery_status"))
    return _first_text(row.get("alert_status"), row.get("status"))


def _alert_integrity_status(row: Mapping[str, Any]) -> str:
    alert = row.get("alert_result")
    if not isinstance(alert, Mapping):
        return UNVERIFIED
    manifest = alert.get("integrity_manifest")
    if not isinstance(manifest, Mapping):
        return UNVERIFIED
    is_valid = manifest.get("is_valid")
    if is_valid is True:
        return "valid"
    if is_valid is False:
        return "review"
    return UNVERIFIED


def _alert_invalidation(row: Mapping[str, Any]) -> str:
    return _short_text(
        _first_text(
            _nested_value(row, "trade_idea", "invalidation"),
            _nested_value(row, "alert_result", "invalidation"),
            row.get("invalidation"),
        )
    )


def _alert_risk_warning(row: Mapping[str, Any]) -> str:
    return _short_text(
        _first_text(
            _nested_value(row, "trade_idea", "risk_warning"),
            _nested_value(row, "alert_result", "risk_warning"),
            row.get("risk_warning"),
        )
    )


def _entry_text(row: Mapping[str, Any]) -> str:
    trade_idea = row.get("trade_idea")
    if isinstance(trade_idea, Mapping):
        zone = _level_text(trade_idea.get("entry_zone"))
        if zone != NA:
            return zone
        low = _display(trade_idea.get("entry_low"))
        high = _display(trade_idea.get("entry_high"))
        if low != NA and high != NA:
            return low if low == high else f"{low} - {high}"
        if low != NA:
            return low
        if high != NA:
            return high
    return _first_text(_level_text(row.get("entry_zone")), row.get("entry"), row.get("entry_price"))


def _stop_text(row: Mapping[str, Any]) -> str:
    return _first_text(
        _level_text(_nested_value(row, "trade_idea", "stop_loss")),
        _level_text(row.get("stop_loss")),
        row.get("stop"),
    )


def _targets_text(row: Mapping[str, Any]) -> str:
    trade_idea = row.get("trade_idea")
    if isinstance(trade_idea, Mapping):
        text = _target_sequence_text(trade_idea.get("take_profits"))
        if text != NA:
            return text
        text = _target_sequence_text(trade_idea.get("take_profit_targets"))
        if text != NA:
            return text
    for key in ("take_profits", "take_profit_targets", "targets"):
        text = _target_sequence_text(row.get(key))
        if text != NA:
            return text
    return _first_text(row.get("tp1"), row.get("target"))


def _updated_text(row: Mapping[str, Any]) -> str:
    return _first_text(row.get("updated_at"), row.get("timestamp"), row.get("last_seen_at"))


def _watch_waiting_for_text(row: Mapping[str, Any]) -> str:
    text = _next_step_text(_row_next_trigger(row))
    if text != NA:
        lowered = text.lower()
        if lowered.startswith("waiting for "):
            value = text[12:].strip().rstrip(".")
            return value[:1].upper() + value[1:] if value else NA
        return text
    return _row_short_reason(row)


def _invalidation_text(row: Mapping[str, Any]) -> str:
    return _first_text(
        _nested_value(row, "trade_idea", "invalidation"),
        row.get("invalidation"),
        row.get("cancel_condition"),
        _diagnostic_value(row, "watchlist_invalidation"),
    )


def _level_text(value: Any) -> str:
    if isinstance(value, Mapping):
        price = _display(value.get("price"))
        low = _display(value.get("low"))
        high = _display(value.get("high"))
        if price != NA:
            return price
        if low != NA and high != NA:
            return low if low == high else f"{low} - {high}"
        if low != NA:
            return low
        if high != NA:
            return high
    return _display(value)


def _target_sequence_text(value: Any) -> str:
    if isinstance(value, str):
        return _display(value)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, Mapping)):
        return NA
    targets: list[str] = []
    for item in value:
        text = _level_text(item)
        if text != NA:
            targets.append(text)
    return ", ".join(targets) if targets else NA


def _status_history(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (_status_key(value),)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Mapping)):
        return tuple(_status_key(item) for item in value if _status_key(item))
    return ()


def _status_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.strip().replace("-", "_").replace(" ", "_").lower()
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


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


def _safety_reason_text(row: Mapping[str, Any]) -> str:
    reason = _target_reason(row)
    if reason != NA:
        return reason
    failure = _target_failure_type(row)
    if failure != NA:
        return _finding_text(failure)
    return "Safety check did not pass."


def _setup_status_text(row: Mapping[str, Any]) -> str:
    status = _display_status(row)
    if status == "valid_setup" or _display(row.get("display_bucket")) == "valid":
        return "Confirmed setup"
    if status == "near_miss" or _display(row.get("display_bucket")) == "near_miss":
        return "Monitoring"
    return _title_text(status)


def _lifecycle_text(row: Mapping[str, Any]) -> str:
    state = _title_text(row.get("lifecycle_current_state"))
    if state != NA:
        return state
    integrity = _display(row.get("lifecycle_integrity_status"))
    if integrity == "STALE_OR_DEGRADED":
        return "Review needed"
    if integrity != NA:
        return _title_text(integrity)
    return NA


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


def _market_universe_text(*sources: Mapping[str, Any]) -> str:
    text = _universe_text(*sources)
    if text == NA:
        return NA
    cleaned = text.replace("_", " ").replace("-", " ")
    cleaned = cleaned.replace(" (manual)", "").replace("(manual)", "")
    cleaned = cleaned.replace("universe", "list").replace("Universe", "List")
    return _title_text(cleaned)


def _symbol_list_text(*sources: Mapping[str, Any]) -> str:
    for source in sources:
        mode = _first_text(source.get("universe_mode"), _nested_value(source, "universe", "mode"))
        if _status_key(mode) == "manual":
            return "Manual"
    text = _market_universe_text(*sources)
    if _status_key(text) == "manual_test_list":
        return "Manual symbol list"
    return text


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


def _title_text(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    normalized = text.replace("_", " ").replace("-", " ").strip()
    if not normalized:
        return NA
    words = []
    for word in normalized.split():
        if word.upper() in {"N/A", "NA"}:
            words.append("N/A")
        elif word.isupper() and len(word) <= 5:
            words.append(word[:1] + word[1:].lower())
        else:
            words.append(word[:1].upper() + word[1:].lower())
    return " ".join(words)


def _delivery_text(value: Any) -> str:
    key = _status_key(value)
    if key == "dry_run":
        return "Test mode"
    if key in {"sent", "sent_admin"}:
        return "Sent"
    if key.startswith("skipped"):
        return "Skipped"
    if key == "failed":
        return "Failed"
    return _title_text(value)


def _integrity_text(value: Any) -> str:
    key = _status_key(value)
    if key == "valid":
        return "Passed"
    if key == "review":
        return "Review needed"
    if key == _status_key(UNVERIFIED):
        return UNVERIFIED
    return _title_text(value)


def _next_step_text(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    lowered = text.lower()
    if "trust" in lowered:
        return "Waiting for stronger confirmation"
    if lowered.startswith("wait for "):
        return "Waiting for " + text[9:].strip().rstrip(".")
    if lowered.startswith("wait "):
        return "Waiting " + text[5:].strip().rstrip(".")
    return text


def _preset_name_text(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    custom = {
        "l1_l2": "L1 / L2",
        "sol_ecosystem": "Sol ecosystem",
        "meme_high_liquidity": "High-liquidity meme",
        "large_caps": "Large caps",
    }
    return custom.get(text, _title_text(text))


def _finding_text(value: Any) -> str:
    key = _status_key(value)
    if key == "missing_integrity_manifest":
        return "Missing safety data"
    if key == "manifest_message_sha256_mismatch":
        return "Message safety check mismatch"
    if key == "manifest_payload_sha256_mismatch":
        return "Alert safety check mismatch"
    if key == "rr_below_minimum":
        return "Reward/risk below minimum"
    if key == "target_integrity":
        return "Target safety check"
    return _title_text(value)


def _enabled_disabled_na(config: Any | None, name: str) -> str:
    if config is None:
        return NA
    value = getattr(config, name, None)
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    return NA


def _active_inactive_na(config: Any | None, name: str) -> str:
    if config is None:
        return NA
    value = getattr(config, name, None)
    if isinstance(value, bool):
        return "Active" if value else "Inactive"
    return NA


def _hidden_status(config: Any | None, name: str) -> str:
    if config is None:
        return NA
    return "Hidden" if _display(getattr(config, name, None)) != NA else "N/A"


def _public_url(config: Any | None, name: str, *, fallback: str) -> str:
    if config is None:
        return fallback
    return _first_text(getattr(config, name, NA), fallback)


def _public_config_text(config: Any | None, name: str, *, fallback: str) -> str:
    if config is None:
        return fallback
    return _first_text(getattr(config, name, NA), fallback)


def _format_public_donate_address_response(
    config: Any | None,
    *,
    title: str,
    address_name: str,
    network: str,
    send_warning: str,
) -> str:
    address = _public_config_text(config, address_name, fallback=NA)
    if address == NA:
        return "Not configured yet."
    return _screen(
        title,
        (
            "Address:",
            address,
            "",
            f"Network: {network}",
            send_warning,
        ),
    )


def _public_logo_path(config: Any | None, project_root: Path) -> Path | None:
    if config is None:
        return None
    text = _display(getattr(config, "public_logo_path", NA))
    if text == NA:
        return None
    path = Path(text)
    candidate = path if path.is_absolute() else project_root / path
    try:
        resolved = candidate.resolve(strict=False)
        return resolved if resolved.is_file() else None
    except (OSError, ValueError):
        return None


def _public_logo_url(config: Any | None) -> str | None:
    if config is None:
        return None
    text = _display(getattr(config, "public_logo_url", NA))
    return None if text == NA else text


def _config_bool(config: Any | None, name: str) -> str:
    if config is None:
        return UNVERIFIED
    value = getattr(config, name, None)
    if isinstance(value, bool):
        return "true" if value else "false"
    return UNVERIFIED


def _config_enabled(config: Any | None, name: str) -> bool:
    if config is None:
        return False
    return bool(getattr(config, name, False))


def _config_presence(config: Any | None, name: str) -> str:
    if config is None:
        return UNVERIFIED
    value = getattr(config, name, None)
    return "configured" if _display(value) != NA else "not configured"


def _config_value(config: Any | None, name: str) -> str:
    if config is None:
        return UNVERIFIED
    return _display(getattr(config, name, NA))


def _short_text(value: Any, max_length: int = 120) -> str:
    text = " ".join(_display(value).split())
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3].rstrip()}..."


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
    "ADMIN_CALLBACK_COMMANDS",
    "ADMIN_COMMANDS",
    "ADMIN_MENU_BUTTON_CALLBACKS",
    "ADMIN_MENU_BUTTON_ROWS",
    "ADMIN_WOLF_BRIEFING_CALLBACK_COMMANDS",
    "PUBLIC_ADMIN_RESERVED_COMMANDS",
    "PUBLIC_CALLBACK_COMMANDS",
    "PUBLIC_COMMANDS",
    "JOIN_SIGNAL_CHANNEL_BUTTON_LABEL",
    "PUBLIC_MENU_BUTTON_CALLBACKS",
    "PUBLIC_MENU_BUTTON_ROWS",
    "SIMPLE_REPLY_BUTTON_COMMANDS",
    "DEFAULT_ADMIN_COMMAND_ROW_LIMIT",
    "DEFAULT_SCAN_RUN_MANIFEST_PATH",
    "SCREEN_DIVIDER",
    "SCREEN_FOOTER",
    "SCREEN_HEADER",
    "WOLF_BRIEFING_CANCEL_BUTTON_LABEL",
    "WOLF_BRIEFING_CANCEL_COMMAND",
    "WOLF_BRIEFING_PREVIEW_HEADER",
    "WOLF_BRIEFING_PUBLISH_BUTTON_LABEL",
    "WOLF_BRIEFING_PUBLISH_COMMAND",
    "WOLF_BRIEFING_REFRESH_BUTTON_LABEL",
    "WATCHLIST_BACK_BUTTON_LABEL",
    "WATCHLIST_REFRESH_BUTTON_LABEL",
    "SIGNAL_DETAIL_BACK_BUTTON_LABEL",
    "SIGNAL_DETAIL_LIFECYCLE_BUTTON_LABEL",
    "SIGNAL_DETAIL_REFRESH_BUTTON_LABEL",
    "SIGNAL_DETAIL_WHY_VALID_BUTTON_LABEL",
    "AdminCommandResponse",
    "LatestScanArtifacts",
    "TelegramAdminCommandService",
    "admin_back_to_menu_inline_markup",
    "admin_menu_inline_markup",
    "admin_menu_reply_markup",
    "admin_watchlists_inline_markup",
    "command_for_callback_data",
    "format_help_response",
    "format_menu_response",
    "format_about_response",
    "format_public_about_response",
    "format_public_admin_reserved_response",
    "format_public_donate_response",
    "format_public_donate_btc_address_response",
    "format_public_donate_ton_address_response",
    "format_public_donate_usdt_ton_address_response",
    "format_public_help_response",
    "format_public_menu_response",
    "format_public_social_response",
    "format_start_response",
    "format_wolf_briefing_preview",
    "load_latest_manifest_row",
    "normalize_admin_command",
    "public_active_signals_inline_markup",
    "public_back_to_menu_inline_markup",
    "public_donate_inline_markup",
    "public_menu_inline_markup",
    "public_menu_reply_markup",
    "public_watchlists_inline_markup",
    "reply_keyboard_remove_markup",
    "signal_detail_inline_markup",
    "wolf_briefing_preview_inline_markup",
]
