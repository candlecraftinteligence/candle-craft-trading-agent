from __future__ import annotations

import asyncio
import json
import socket
from decimal import Decimal
from typing import Any

from app.agents.alert_agent import AlertAgent
from app.agents.trade_idea import TradeIdeaResult, create_trade_idea
from app.alerts.integrity_manifest import (
    ALERT_INTEGRITY_MANIFEST_SCHEMA_VERSION,
    audit_alert_integrity_artifact,
)
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult
from app.watch_mode import build_watch_activation_alert_manifest, format_watch_activation_alert
from scripts import audit_alert_integrity_manifest as alert_integrity_cli


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _idea(**overrides: object) -> TradeIdeaResult:
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "market_type": "perpetual",
        "direction": "long",
        "timeframe": "15m",
        "setup_type": "liquidity_grab_pullback_swing",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop_loss": Decimal("95"),
        "take_profit_targets": (Decimal("112"), Decimal("120")),
        "invalidation": "Invalid below 95.",
        "opportunity_score": Decimal("88"),
        "opportunity_grade": "A",
        "opportunity_decision": "high_quality_candidate",
        "risk_approved": True,
        "best_rr": Decimal("3.2"),
        "technical_summary": "Bullish sweep, confirmation, and pullback.",
        "derivatives_summary": "Derivatives support the long.",
        "confirmed_facts": ("Sweep confirmed.",),
        "cancel_condition": "Cancel if price closes below 95.",
    }
    data.update(overrides)
    return create_trade_idea(data)


def _alert_payload(*, include_manifest: bool = True) -> dict[str, object]:
    idea = _idea()
    result = run(
        AlertAgent().send(
            {
                "trade_idea": idea,
                "deduplication_key": "BTCUSDT-15m-liquidity_grab_pullback_swing",
            }
        )
    )
    payload = result.model_dump(mode="json")
    if not include_manifest:
        payload.pop("integrity_manifest", None)
    return payload


def _scanner_payload(alert_result: dict[str, object], *, trade_idea: TradeIdeaResult | None = None) -> dict[str, object]:
    return {
        "run_id": "run-001",
        "timestamp": "2026-06-01T00:00:00+00:00",
        "results": [
            {
                "symbol": "BTCUSDT",
                "status": "journal_entry_created",
                "status_history": ["idea_created", "alert_dry_run_created", "journal_entry_created"],
                "trade_idea": (trade_idea or _idea()).model_dump(mode="json"),
                "alert_result": alert_result,
            }
        ],
    }


def test_alert_agent_attaches_deterministic_integrity_manifest() -> None:
    result = run(
        AlertAgent().send(
            {
                "trade_idea": _idea(),
                "deduplication_key": "BTCUSDT-15m-liquidity_grab_pullback_swing",
            }
        )
    )

    manifest = result.integrity_manifest
    assert manifest is not None
    assert manifest.schema_version == ALERT_INTEGRITY_MANIFEST_SCHEMA_VERSION
    assert manifest.is_valid is True
    assert manifest.dry_run is True
    assert manifest.required_field_status["invalidation"] is True
    assert manifest.required_field_status["risk_warning"] is True
    assert manifest.safety_checks["message_has_risk_warning"] is True
    assert manifest.safety_checks["message_has_invalidation"] is True

    repeated = run(
        AlertAgent().send(
            {
                "trade_idea": _idea(),
                "deduplication_key": "BTCUSDT-15m-liquidity_grab_pullback_swing",
            }
        )
    )
    assert repeated.integrity_manifest is not None
    assert repeated.integrity_manifest.payload_sha256 == manifest.payload_sha256


def test_manifest_does_not_copy_telegram_credentials() -> None:
    result = run(
        AlertAgent().send(
            {
                "trade_idea": _idea(),
                "channel": "telegram",
                "telegram_bot_token": "secret-token",
                "telegram_chat_id": "secret-chat",
                "deduplication_key": "BTCUSDT-15m-liquidity_grab_pullback_swing",
            }
        )
    )

    payload = json.dumps(result.integrity_manifest.model_dump(mode="json"))  # type: ignore[union-attr]
    assert "secret-token" not in payload
    assert "secret-chat" not in payload


def test_legacy_alert_without_manifest_is_warning_not_signal_change() -> None:
    result = audit_alert_integrity_artifact(_scanner_payload(_alert_payload(include_manifest=False)))
    codes = {issue.code for issue in result.issues}

    assert result.summary.alert_count == 1
    assert result.summary.is_valid is True
    assert "missing_integrity_manifest" in codes


def test_tampered_manifest_hash_is_blocked() -> None:
    alert_payload = _alert_payload()
    manifest = dict(alert_payload["integrity_manifest"])  # type: ignore[arg-type]
    manifest["message_sha256"] = "bad-hash"
    alert_payload["integrity_manifest"] = manifest

    result = audit_alert_integrity_artifact(_scanner_payload(alert_payload))
    codes = {issue.code for issue in result.issues}

    assert result.summary.is_valid is False
    assert "manifest_message_sha256_mismatch" in codes


def test_alert_without_trade_idea_or_idea_status_is_blocked() -> None:
    payload = {
        "results": [
            {
                "symbol": "BTCUSDT",
                "status": "alert_dry_run_created",
                "status_history": ["alert_dry_run_created"],
                "alert_result": _alert_payload(include_manifest=False),
            }
        ]
    }

    result = audit_alert_integrity_artifact(payload)
    codes = {issue.code for issue in result.issues}

    assert result.summary.is_valid is False
    assert "alert_without_trade_idea" in codes
    assert "alert_without_idea_status_history" in codes


def test_watch_activation_alert_includes_invalidation_risk_warning_and_manifest() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        status_history=(
            ScannerPipelineStatus.IDEA_CREATED,
            ScannerPipelineStatus.ALERT_DRY_RUN_CREATED,
            ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        ),
        trade_idea=_idea(),
        valid_strategy_modes=("swing",),
    )

    message = format_watch_activation_alert(symbol_result)
    manifest = build_watch_activation_alert_manifest(
        symbol_result,
        message=message,
        delivery_status="dry_run",
        live=False,
    )

    assert "WATCHLIST UPGRADED" in message
    assert "The wolf has confirmation." in message
    assert "Previous state: WATCHLIST" in message
    assert "New state: CONFIRMED SIGNAL" in message
    assert manifest.is_valid is True
    assert manifest.deduplication_key == "watch-activation-BTCUSDT-SWING"


def test_cli_json_emits_alert_integrity_summary(tmp_path, capsys) -> None:
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(_scanner_payload(_alert_payload())), encoding="utf-8")

    exit_code = alert_integrity_cli.main(["--json", str(path)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["summary"]["alert_count"] == 1
    assert payload["summary"]["manifest_count"] == 1


def test_default_alert_manifest_artifacts_exclude_deprecated_watch_state(tmp_path) -> None:
    project_root = tmp_path
    scan_runs = project_root / "scan_runs"
    scan_runs.mkdir()
    latest_scan = scan_runs / "latest_scan.json"
    watch_state = scan_runs / "watch_state.json"
    latest_scan.write_text("{}", encoding="utf-8")
    watch_state.write_text("{}", encoding="utf-8")

    paths = alert_integrity_cli.default_artifact_paths(project_root)

    assert latest_scan in paths
    assert watch_state not in paths


def test_audit_does_not_require_network_calls(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("alert integrity audit must not call network transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = audit_alert_integrity_artifact(_scanner_payload(_alert_payload()))

    assert result.summary.error_count == 0
