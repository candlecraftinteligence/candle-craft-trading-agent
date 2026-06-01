from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal

import pytest

from app.analytics.setup_quality import validate_setup_quality
from app.analytics.target_intelligence import TargetFailureType, TargetIntelligenceResult, TargetQualityGrade
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.telegram_admin import (
    TelegramAdminConfig,
    build_admin_drafts,
    format_admin_scan_report,
    persist_admin_drafts,
    route_admin_scan_report,
)


class FakeAdminTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def send_message(self, *, bot_token: str, chat_id: str, message: str):
        self.calls.append({"bot_token": bot_token, "chat_id": chat_id, "message": message})
        return (
            {
                "status": "sent",
                "part_number": 1,
                "total_parts": 1,
                "error": None,
                "message_id": 321,
                "chat_id": chat_id,
                "sent_at": "2026-06-01T12:00:01Z",
                "bot_token": bot_token,
                "authorization": f"Bearer {bot_token}",
            },
        )


class FailedAdminTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def send_message(self, *, bot_token: str, chat_id: str, message: str):
        self.calls.append({"bot_token": bot_token, "chat_id": chat_id, "message": message})
        return (
            {
                "status": "failed",
                "part_number": 1,
                "total_parts": 1,
                "error": f"Telegram rejected {bot_token} for {chat_id}",
            },
        )


def _config(symbols: tuple[str, ...]) -> ScannerRunConfig:
    return ScannerRunConfig.model_validate(
        {
            "symbols": list(symbols),
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )


def _run_result(results: tuple[ScannerSymbolResult, ...], *, run_id: str = "run-admin") -> ScannerRunResult:
    return ScannerRunResult(
        config=_config(tuple(result.symbol for result in results)),
        results=results,
        scanned_symbols=len(results),
        failed_symbols=0,
        trade_ideas_created=sum(1 for result in results if result.trade_idea is not None),
        dry_run_alerts_created=sum(
            1 for result in results if ScannerPipelineStatus.ALERT_DRY_RUN_CREATED in result.status_history
        ),
        journal_entries_created=sum(1 for result in results if result.journal_entry is not None),
        resume_metadata={
            "run_id": run_id,
            "scan_run_id": run_id,
            "universe": {"mode": "manual", "label": "manual test universe"},
        },
    )


def _valid_result(symbol: str = "VALIDUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        technical_score=86,
        derivatives_score=84,
        strategy_diagnostics={
            "swing": {
                "is_valid": True,
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
                "trust_grade": "A",
                "trust_percentage": 88,
                "rr_to_tp2": Decimal("3.2"),
                "derivatives_supports_trade": True,
            }
        },
        valid_strategy_modes=("swing",),
        setup_quality=validate_setup_quality(
            {
                "symbol": symbol,
                "setup_valid": True,
                "mode": "swing",
                "bias": "long",
                "sweep_passed": True,
                "confirmation_passed": True,
                "pullback_valid": True,
                "rr_to_tp2": Decimal("3.2"),
                "trust_percentage": 88,
                "derivatives_supports_trade": True,
                "derivatives_score": 84,
            }
        ),
    )


def _near_result(symbol: str = "NEARUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejection_reason="Trust meter is below minimum.",
        rejection_stage="strategy",
        rejection_reasons=("Trust meter is below minimum.",),
        technical_score=74,
        derivatives_score=76,
        strategy_diagnostics={
            "swing": {
                "is_valid": False,
                "bias": "short",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "first_failed_gate": "trust_meter_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("trust_meter_below_minimum",),
                "hard_rejection_reasons": ("Trust meter is below minimum.",),
                "trust_percentage": 62,
                "rr_to_tp2": Decimal("2.6"),
                "derivatives_supports_trade": True,
            }
        },
        rejected_strategy_modes=("swing",),
    )


def _target_blocked_result(symbol: str = "TARGETUSDT") -> ScannerSymbolResult:
    target = TargetIntelligenceResult(
        target_quality_grade=TargetQualityGrade.REJECT,
        target_failure_type=TargetFailureType.RR_BELOW_MINIMUM,
        rr_compression_reason="Clean target path is too compressed.",
        next_target_condition="Wait for target expansion above opposing structure.",
    )
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejection_reason="Clean target path is too compressed.",
        rejection_stage="target_integrity",
        strategy_diagnostics={
            "swing": {
                "is_valid": False,
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "first_failed_gate": "target_integrity",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr"),
                "gates_failed": ("target_integrity",),
                "target_integrity_status": "blocked",
                "target_integrity_reason": "Clean target path is too compressed.",
            }
        },
        rejected_strategy_modes=("swing",),
        target_intelligence=target,
    )


def _lifecycle_degraded_result(symbol: str = "STALEUSDT") -> ScannerSymbolResult:
    lifecycle = SetupLifecycleRecord(
        lifecycle_id=f"{symbol.lower()}-swing",
        symbol=symbol,
        mode="swing",
        direction="long",
        current_state=SetupLifecycleState.TRIGGERED,
        previous_state=SetupLifecycleState.CONFIRMED,
        first_seen_at="2026-06-01T00:00:00+00:00",
        last_seen_at="2026-06-01T00:05:00+00:00",
        last_transition_at="2026-06-01T00:05:00+00:00",
        failed_gate=NA,
        readiness_score=86,
        quality_score=84,
        action_label="Trade candidate",
    )
    return _near_result(symbol).model_copy(update={"lifecycle_state": lifecycle})


def _manifest(run_id: str = "run-admin") -> dict[str, object]:
    return {
        "run_id": run_id,
        "timestamp": "2026-06-01T12:00:00+00:00",
        "universe_label": "manual test universe",
        "market_regime": "balanced",
        "regime_confidence": 72,
        "symbols_scanned": 1,
        "runtime_seconds": 1.25,
    }


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_disabled_admin_persists_local_drafts_without_network(tmp_path) -> None:
    transport = FakeAdminTransport()
    result = _run_result((_near_result(),))

    routed = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=_manifest(),
            config=TelegramAdminConfig(admin_enabled=False, dry_run=True),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )

    assert routed.delivery_status == "skipped_disabled"
    assert transport.calls == []
    assert routed.draft_path is not None
    records = _read_jsonl(routed.draft_path)
    assert {record["delivery_status"] for record in records} == {"skipped_disabled"}


def test_missing_credentials_do_not_crash_and_skip_send(tmp_path) -> None:
    transport = FakeAdminTransport()
    result = _run_result((_near_result(),))

    routed = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=_manifest(),
            config=TelegramAdminConfig(admin_enabled=True, dry_run=False),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )

    assert routed.delivery_status == "skipped_missing_credentials"
    assert "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_ADMIN_CHAT_ID" in routed.warning
    assert transport.calls == []
    assert routed.draft_path is not None
    assert routed.draft_path.exists()


@pytest.mark.parametrize(
    ("bot_token", "admin_chat_id"),
    (
        (None, "admin-chat"),
        ("secret-token", None),
    ),
)
def test_missing_each_admin_credential_skips_without_leaking_secrets(
    tmp_path,
    caplog,
    bot_token,
    admin_chat_id,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.telegram_admin.draft_router")
    transport = FakeAdminTransport()
    result = _run_result((_near_result(),))

    routed = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=_manifest(),
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token=bot_token,
                admin_chat_id=admin_chat_id,
            ),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )

    assert routed.delivery_status == "skipped_missing_credentials"
    assert transport.calls == []
    assert routed.error_message == "missing_telegram_admin_credentials"
    assert "secret-token" not in caplog.text


def test_dry_run_mode_persists_local_drafts_without_network(tmp_path) -> None:
    transport = FakeAdminTransport()
    result = _run_result((_valid_result(),))

    routed = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=_manifest(),
            config=TelegramAdminConfig(admin_enabled=True, dry_run=True, bot_token="secret", admin_chat_id="admin"),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )

    assert routed.delivery_status == "dry_run"
    assert transport.calls == []
    records = _read_jsonl(routed.draft_path)
    assert "valid_setup" in {record["draft_type"] for record in records}


def test_enabled_admin_mode_sends_one_admin_report_and_ignores_public_vip(tmp_path) -> None:
    transport = FakeAdminTransport()
    result = _run_result((_valid_result(),))

    routed = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=_manifest(),
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="public-channel",
                vip_channel_id="vip-channel",
            ),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )

    assert routed.delivery_status == "sent_admin"
    assert len(transport.calls) == 1
    assert transport.calls[0]["chat_id"] == "admin-chat"
    assert "public-channel" not in transport.calls[0]["message"]
    assert "vip-channel" not in transport.calls[0]["message"]
    assert "Admin-only. No public/VIP send." in transport.calls[0]["message"]
    assert "Draft artifact:" in transport.calls[0]["message"]


def test_live_send_success_records_safe_telegram_metadata(tmp_path) -> None:
    transport = FakeAdminTransport()
    result = _run_result((_valid_result(),))

    routed = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=_manifest(),
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )

    assert routed.delivery_status == "sent_admin"
    records = _read_jsonl(routed.draft_path)
    assert {record["delivery_status"] for record in records} == {"sent_admin"}
    metadata = records[0]["telegram_metadata"]
    assert metadata["message_id"] == 321
    assert metadata["chat_id"] == "admin-chat"
    assert metadata["sent_at"] == "2026-06-01T12:00:01Z"
    serialized = json.dumps(records)
    assert "secret-token" not in serialized
    assert "authorization" not in serialized


def test_live_send_failure_is_non_fatal_and_redacts_secrets(tmp_path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="app.telegram_admin.draft_router")
    transport = FailedAdminTransport()
    result = _run_result((_valid_result(),))

    routed = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=_manifest(),
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )

    assert routed.delivery_status == "failed"
    assert "[REDACTED]" in routed.error_message
    assert "secret-token" not in routed.error_message
    assert "admin-chat" not in routed.error_message
    records = _read_jsonl(routed.draft_path)
    assert {record["delivery_status"] for record in records} == {"failed"}
    assert "secret-token" not in caplog.text
    assert "admin-chat" not in caplog.text


def test_live_send_is_not_duplicated_for_same_run_id(tmp_path) -> None:
    transport = FakeAdminTransport()
    result = _run_result((_valid_result(),), run_id="run-duplicate")
    config = TelegramAdminConfig(
        admin_enabled=True,
        dry_run=False,
        bot_token="secret-token",
        admin_chat_id="admin-chat",
    )

    first = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=_manifest("run-duplicate"),
            config=config,
            transport=transport,
            drafts_dir=tmp_path,
        )
    )
    second = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=_manifest("run-duplicate"),
            config=config,
            transport=transport,
            drafts_dir=tmp_path,
        )
    )

    assert first.delivery_status == "sent_admin"
    assert second.delivery_status == "sent_admin"
    assert "duplicate live send skipped" in second.delivery_detail
    assert len(transport.calls) == 1
    assert second.drafts_created == 0


def test_live_send_after_dry_run_updates_drafts_then_dedupes(tmp_path) -> None:
    transport = FakeAdminTransport()
    result = _run_result((_valid_result(),), run_id="run-dry-to-live")
    manifest = _manifest("run-dry-to-live")

    dry = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=manifest,
            config=TelegramAdminConfig(admin_enabled=True, dry_run=True),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )
    live = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=manifest,
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )
    duplicate = asyncio.run(
        route_admin_scan_report(
            result,
            manifest_row=manifest,
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )

    assert dry.delivery_status == "dry_run"
    assert live.delivery_status == "sent_admin"
    assert duplicate.delivery_status == "sent_admin"
    assert len(transport.calls) == 1
    assert live.drafts_created == dry.drafts_created
    assert duplicate.drafts_created == 0
    records = _read_jsonl(dry.draft_path)
    assert {record["delivery_status"] for record in records} == {"dry_run", "sent_admin"}


def test_admin_report_includes_target_blocked_lifecycle_and_no_trade_footer() -> None:
    result = _run_result(
        (
            _valid_result(),
            _near_result(),
            _target_blocked_result(),
            _lifecycle_degraded_result(),
        )
    )

    report = format_admin_scan_report(result, manifest_row=_manifest())

    assert "Valid Setups" in report
    assert "VALIDUSDT | long" in report
    assert "Near Misses" in report
    assert "NEARUSDT | final" in report
    assert "Target Blocked" in report
    assert "TARGETUSDT | RR_BELOW_MINIMUM | Clean target path is too compressed." in report
    assert "Lifecycle Degraded" in report
    assert "STALEUSDT | TRIGGERED" in report
    assert "No valid setup = no trade." in report
    assert "Admin-only. No public/VIP send." in report
    assert "No execution behavior enabled." in report


def test_no_valid_setup_report_keeps_no_trade_warning() -> None:
    result = _run_result((_near_result(),))

    report = format_admin_scan_report(result, manifest_row=_manifest())

    assert "Valid setups: 0" in report
    assert "No valid setup = no trade." in report


def test_diagnostic_target_intelligence_does_not_create_target_blocked_draft() -> None:
    diagnostic_target = TargetIntelligenceResult(
        target_quality_grade=TargetQualityGrade.C,
        target_failure_type=NA,
        next_target_condition="N/A",
    )
    result = _run_result((_near_result().model_copy(update={"target_intelligence": diagnostic_target}),))

    report = format_admin_scan_report(result, manifest_row=_manifest())
    drafts = build_admin_drafts(
        result,
        manifest_row=_manifest(),
        delivery_status="dry_run",
        created_at="2026-06-01T12:00:00Z",
    )

    assert "Target blocked: 0" in report
    assert "target_blocked" not in {draft.draft_type for draft in drafts}


def test_deduplication_prevents_duplicate_drafts_for_same_run_symbol_type(tmp_path) -> None:
    result = _run_result((_near_result(),))
    drafts = build_admin_drafts(
        result,
        manifest_row=_manifest(),
        delivery_status="dry_run",
        created_at="2026-06-01T12:00:00Z",
    )

    first = persist_admin_drafts(drafts, drafts_dir=tmp_path)
    second = persist_admin_drafts(drafts, drafts_dir=tmp_path)

    assert first.created == len(drafts)
    assert second.created == 0
    assert second.skipped_duplicates == len(drafts)
    records = _read_jsonl(first.path)
    assert len(records) == len(drafts)
